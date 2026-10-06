#include "lr_store.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <zephyr/bluetooth/conn.h>
#include <zephyr/bluetooth/gatt.h>
#include <zephyr/bluetooth/uuid.h>
#include <zephyr/fs/fs.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/random/random.h>
#include <zephyr/sys/byteorder.h>
#include <zephyr/sys/ring_buffer.h>

#include "lr_log.h"
#include "sdcard.h"
#include "usb.h"
#include "transport.h"

LOG_MODULE_REGISTER(lr_store, CONFIG_LOG_DEFAULT_LEVEL);

extern struct k_mutex write_sdcard_mutex;  // transport.c: one SPI bus, one writer at a time
extern uint32_t transport_frame_no;        // transport.c: frames the codec has made since boot

#define DIR "/SD:/LR"
#define REC_FRAMES 10        // a record per 100 ms: a phone following the stream gets each one as it closes
#define SEG_MAX (16u << 20)  // a long offline stretch still ends in files a phone can take one by one
#define CHUNK 4096           // SD read size while sending a file
#define RESERVE_KB CONFIG_LR_STORE_RESERVE_KB  // recording stops (-ENOSPC) below this much free space, resumes at twice it
#define BATCH CONFIG_LR_STORE_BATCH            // finished records kept in RAM until there are this many bytes

static uint16_t boot;  // 0: no card at boot (nothing to count boots on); the stream still goes to a phone
static int16_t store_err = -ENODEV;  // until lr_store_init succeeds: no card, or it failed
static uint16_t seg;
static uint32_t seg_size;  // the segment's length in the stream: on the card (or lost to it), plus `done` bytes in RAM
static char path[32];
uint32_t free_kb;  // the card, as of the last statvfs, less what was written since (lr_log.c heartbeat too)
static uint32_t total_kb;

/* The record stream in RAM: `done` bytes of finished records not yet on the card, then the open 'A' record. Every
 * record the card gets goes to a following phone too (LIVE), with the same file and offset, as it closes. */
static uint8_t pend[BATCH + 6 + REC_FRAMES * 256];
static uint32_t done, rec_len, rec_next;  // finished bytes; bytes of the open record; the frame that may extend it
static uint8_t rec_count;

static const char *file_path(char *buf, uint32_t file)
{
    snprintf(buf, 32, DIR "/%08X.BIN", file);
    return buf;
}

static uint32_t cur_file(void)
{
    return (uint32_t) boot << 16 | seg;
}

static void restart_segment(void);

/* The card's free space; a full card stops recording until a phone deletes what it took. Store thread or SD mutex. */
static void count_free(void)
{
    struct fs_statvfs v;
    if (fs_statvfs("/SD:", &v) == 0) {
        free_kb = (uint64_t) v.f_bfree * v.f_frsize / 1024;
        total_kb = (uint64_t) v.f_blocks * v.f_frsize / 1024;
    }
    if (store_err == -ENOSPC && free_kb >= 2 * RESERVE_KB) {
        store_err = 0;
        restart_segment();  // the stream went on while the card was full: the card starts a file of its own
        LOG_INF("card has room again: %u KB", free_kb);
        lr_log(LR_CARD_FULL, 0, free_kb, 0);
    }
}

uint32_t dropped;            // frames lost on the way to the store (gaps in the frame numbers it got)
static uint16_t append_max;  // the slowest append so far, ms

static struct fs_file_t seg_file;  // the open segment, kept open: reopening walked its FAT chain on every append, slower
static bool seg_open;              // as the file grew (4 KB clusters): ~0.5 s per append at 6 MB, boot 6 of 10-03

static void close_segment(void)
{
    if (seg_open) {
        fs_close(&seg_file);
        seg_open = false;
    }
}

static bool append_failed;  // only the first failure of a run goes to the event log

/* Appends summed up for 10 minutes into one SD_WRITES event: how long the card keeps the chip busy (power, #95) */
static struct {
    int64_t start;
    uint32_t ms, max, n[4];  // under 20 ms, 20-49, 50-200, over 200
} wr;

static void count_write(uint32_t ms)
{
    wr.ms += ms;
    wr.max = MAX(wr.max, ms);
    wr.n[ms < 20 ? 0 : ms < 50 ? 1 : ms <= 200 ? 2 : 3]++;
    int64_t now = k_uptime_get();
    if (now - wr.start >= 10 * 60 * 1000) {
        lr_log(LR_SD_WRITES, MIN(wr.n[3], 0xFF), MIN(wr.ms, 0xFFFFF) | MIN(wr.max, 0xFFF) << 20,
               MIN(wr.n[0], 0x3FF) | MIN(wr.n[1], 0x3FF) << 10 | MIN(wr.n[2], 0x3FF) << 20);
        memset(&wr, 0, sizeof(wr));
        wr.start = now;
    }
}

/* The next segment, if this one has anything. A file on the card is always the start of the stream's file of that
 * name: after bytes the card did not take (no card, full, lent, a failed write) it goes on in a new one. */
static void restart_segment(void)
{
    close_segment();
    if (seg_size) {
        // finished records not on the card are the old file's (LIVE sent them so): not the new one's start
        memmove(pend, pend + done, rec_len);
        done = 0;
        seg++;
        seg_size = 0;
        lr_log(LR_SEGMENT, 0, cur_file(), 0);
    }
    file_path(path, cur_file());
}

static int append(const void *data, uint32_t len)
{
    int64_t t0 = k_uptime_get();
    if (free_kb < RESERVE_KB) {
        LOG_ERR("card full (%u KB free): not recording", free_kb);
        lr_log(LR_CARD_FULL, 1, free_kb, 0);
        return -ENOSPC;
    }
    int err = 0;
    if (!seg_open) {
        fs_file_t_init(&seg_file);
        err = fs_open(&seg_file, path, FS_O_CREATE | FS_O_WRITE | FS_O_APPEND);
        seg_open = !err;
    }
    if (!err) {
        int n = fs_write(&seg_file, data, len);
        fs_sync(&seg_file);  // on the card now: power can go any time
        uint32_t ms = k_uptime_get() - t0;
        append_max = MAX(append_max, (uint16_t) MIN(ms, UINT16_MAX));
        if (ms > 1000) {  // SD_WRITES counts those over 200 ms: one event each filled LR-P1's log in 80 min (#99)
            lr_log(LR_SLOW, 0, ms, len);
        }
        count_write(ms);
        if (n > 0) {
            free_kb -= MIN(free_kb, DIV_ROUND_UP(n, 1024));
        }
        err = n == len ? 0 : n < 0 ? n : -EIO;
    }
    if (err && !append_failed) {
        lr_log(LR_APPEND_ERR, 0, err, len);
    }
    append_failed = err;
    return err;
}

/* The finished records to the card in one write. A card that cannot take them does not stop the stream: the
 * segment ends there, and TIME's store reads the error. */
static void write_out(void)
{
    if (!done) {
        return;
    }
    int err = store_err ? 0 : append(pend, done);
    memmove(pend, pend + done, rec_len);  // the open record moves up
    done = 0;
    if (err) {
        LOG_ERR("SD write failed: %d", err);
        store_err = err;
        close_segment();  // the stream goes on in this segment; the card gets a new one once it works again
    }
}

static void live(const uint8_t *b, uint32_t len, uint32_t offset);

/* A finished record into the stream: to a following phone now, to the card with its batch. */
static void put_record(uint32_t len)
{
    live(pend + done, len, seg_size);
    done += len;
    seg_size += len;
}

static void close_record(void)
{
    if (rec_count) {
        pend[done + 1] = rec_count;
        put_record(rec_len);
        rec_len = rec_count = 0;
    }
}

/* The open record closed and everything finished written: before a READ of the open segment, a reset, a lend. */
static void flush(void)
{
    close_record();
    write_out();
}

static void next_segment(void)
{
    flush();
    restart_segment();
}

void lr_store_frame(uint32_t frame_no, const uint8_t *data, uint16_t len, bool after_gap)
{
    if (len > 255) {
        return;
    }
    if (rec_count && frame_no > rec_next && !after_gap) {
        dropped += frame_no - rec_next;
        lr_log(LR_DROPPED, 0, frame_no - rec_next, dropped);
    }
    if (rec_count && (frame_no != rec_next || rec_count == REC_FRAMES)) {
        close_record();
        if (done >= BATCH) {
            write_out();
        }
    }
    if (seg_size >= SEG_MAX) {
        next_segment();
    }
    if (!rec_count) {
        pend[done] = 'A';
        sys_put_le32(frame_no, pend + done + 2);
        rec_len = 6;
    }
    pend[done + rec_len++] = len;
    memcpy(pend + done + rec_len, data, len);
    rec_len += len;
    rec_count++;
    rec_next = frame_no + 1;
}

/* A 'T' record into the stream (SD mutex held): the phone's clock at the frame the codec is on now. */
static void put_sync(uint32_t frame, uint64_t epoch)
{
    close_record();
    if (done >= BATCH) {
        write_out();
    }
    pend[done] = 'T';
    sys_put_le32(frame, pend + done + 1);
    sys_put_le64(epoch, pend + done + 5);
    put_record(13);
}

//
// Commands, handled on the store's own thread: SD access and notifications may block, the BT RX thread must not
//
enum { LIST = 1, READ = 2, STOP = 3, DELETE = 4, FORGET = 5, USB_DISK = 6, LOG = 7, SYNC = 0x10, UNLEND = 0x12, FOLLOW = 0x13, PAUSE = 0x20 };
struct cmd {
    uint8_t op;
    uint32_t file, offset;
    uint64_t epoch;
    uint32_t frame;
};
K_MSGQ_DEFINE(cmds, sizeof(struct cmd), 8, 4);

/* A phone starts following the stream: it goes on in a new file, so the phone has that one from offset 0 by LIVE and
 * all before it in closed files it READs in the background; a backlog of days does not hold back what is said now
 * (#130). While the card does not take the stream, nothing before this could be READ anyway. */
static void live_ccc(const struct bt_gatt_attr *attr, uint16_t value)
{
    if (value == BT_GATT_CCC_NOTIFY) {
        struct cmd c = {.op = FOLLOW};
        k_msgq_put(&cmds, &c, K_NO_WAIT);
    }
}

static ssize_t time_write(struct bt_conn *conn, const struct bt_gatt_attr *attr, const void *buf, uint16_t len,
                          uint16_t offset, uint8_t flags)
{
    if (len != 8) {
        return BT_GATT_ERR(BT_ATT_ERR_INVALID_ATTRIBUTE_LEN);
    }
    struct cmd c = {.op = SYNC, .epoch = sys_get_le64(buf), .frame = transport_frame_no};
    lr_log(LR_TIME, 0, c.epoch, c.epoch >> 32);
    k_msgq_put(&cmds, &c, K_NO_WAIT);
    return len;
}

/* Why the last boot ended, for a phone to see: RESETREAS, and the fatal error a crash left in RAM that survives a
 * reset (reason, PC; zero when none) */
static uint32_t resetreas, last_fault[2];
static __noinit uint32_t fault[3];  // magic, reason, pc

void k_sys_fatal_error_handler(unsigned int reason, const z_arch_esf_t *esf)
{
    fault[0] = 0xFA017;
    fault[1] = reason;
    fault[2] = esf ? esf->basic.pc : 0;
    NVIC_SystemReset();
}

static ssize_t time_read(struct bt_conn *conn, const struct bt_gatt_attr *attr, void *buf, uint16_t len,
                         uint16_t offset)
{
    extern int16_t usb_diag[4];
    uint8_t v[47];
    sys_put_le16(boot, v);
    sys_put_le32(transport_frame_no, v + 2);
    sys_put_le16(store_err, v + 6);
    sys_put_le32(free_kb, v + 8);
    sys_put_le32(total_kb, v + 12);
    sys_put_le32(resetreas, v + 16);
    sys_put_le32(last_fault[0], v + 20);
    sys_put_le32(last_fault[1], v + 24);
    sys_put_le32(dropped, v + 28);
    sys_put_le16(append_max, v + 32);
    sys_put_le16(usb_diag[0], v + 34);
    sys_put_le16(usb_diag[1], v + 36);
    sys_put_le16(usb_diag[2], v + 38);
    sys_put_le16(usb_diag[3], v + 40);
    v[42] = MIN(lr_bond_count(), UINT8_MAX);
    sys_put_le32(lr_pause_left(), v + 43);
    return bt_gatt_attr_read(conn, attr, buf, len, offset, v, sizeof(v));
}

static ssize_t ctrl_write(struct bt_conn *conn, const struct bt_gatt_attr *attr, const void *buf, uint16_t len,
                          uint16_t offset, uint8_t flags)
{
    const uint8_t *b = buf;
    struct cmd c = {.op = len ? b[0] : 0};
    if (len >= 5) {
        c.file = sys_get_le32(b + 1);
    }
    if (len >= 9) {
        c.offset = sys_get_le32(b + 5);
    }
    if ((c.op == USB_DISK || c.op == FORGET) && len >= 2) {
        c.file = b[1];  // USB disk on (1) / off (0); forget every phone, this one too (1)
    }
    if (c.op == PAUSE && len >= 5) {  // nothing to do with the card: at once, and on a recorder without one too
        lr_pause(sys_get_le32(b + 1));
        return len;
    }
    k_msgq_put(&cmds, &c, K_NO_WAIT);
    return len;
}

#define LR_UUID(n) BT_UUID_DECLARE_128(BT_UUID_128_ENCODE(0x7c3e0000 + (n), 0x1a2b, 0x4c5d, 0x8e9f, 0x6c6976657265))

BT_GATT_SERVICE_DEFINE(lr_svc,
                       BT_GATT_PRIMARY_SERVICE(LR_UUID(0)),
                       BT_GATT_CHARACTERISTIC(LR_UUID(1),
                                              BT_GATT_CHRC_READ | BT_GATT_CHRC_WRITE,
                                              BT_GATT_PERM_READ_ENCRYPT | BT_GATT_PERM_WRITE_ENCRYPT,
                                              time_read,
                                              time_write,
                                              NULL),
                       BT_GATT_CHARACTERISTIC(LR_UUID(2),
                                              BT_GATT_CHRC_WRITE | BT_GATT_CHRC_NOTIFY,
                                              BT_GATT_PERM_WRITE_ENCRYPT,
                                              NULL,
                                              ctrl_write,
                                              NULL),
                       BT_GATT_CCC(NULL, BT_GATT_PERM_READ | BT_GATT_PERM_WRITE_ENCRYPT),
                       BT_GATT_CHARACTERISTIC(LR_UUID(3), BT_GATT_CHRC_NOTIFY, BT_GATT_PERM_NONE, NULL, NULL, NULL),
                       BT_GATT_CCC(NULL, BT_GATT_PERM_READ | BT_GATT_PERM_WRITE_ENCRYPT),
                       BT_GATT_CHARACTERISTIC(LR_UUID(4), BT_GATT_CHRC_NOTIFY, BT_GATT_PERM_NONE, NULL, NULL, NULL),
                       BT_GATT_CCC(live_ccc, BT_GATT_PERM_READ | BT_GATT_PERM_WRITE_ENCRYPT), );
#define CTRL_ATTR (&lr_svc.attrs[4])
#define DATA_ATTR (&lr_svc.attrs[7])
#define LIVE_ATTR (&lr_svc.attrs[10])

/* LIVE goes out on a thread of its own (live_thread). The pusher only queues a finished record here, never waiting for
 * Bluetooth with the SD mutex held: a notify waits for a TX buffer, and a link that stalls (going down: up to the
 * supervision timeout; coming up: MTU, security, PHY) held the pusher until the codec's queue overflowed and frames
 * were lost from the card too (#133: 2.5-4.5 s at each drop of the X1's link). Now a stalled link fills this queue
 * (~2 s) and the records after are left out of LIVE only: the phone READs the gap from the card. One producer (the
 * pusher, SD mutex held), one consumer: no lock (ring_buf_put publishes the whole record at once). */
#define LIVE_QUEUE (8 * 1024)
RING_BUF_DECLARE(live_rb, LIVE_QUEUE);
static K_SEM_DEFINE(live_sem, 0, 1);
struct live_rec {
    uint32_t file, offset;
    uint16_t len;
} __packed;
static uint32_t live_lost, live_lost_bytes;  // records left out of LIVE in a row: logged once one goes again

/* n bytes into the queue, claimed only: the record goes out whole at ring_buf_put_finish */
static void live_claim(const void *d, uint32_t n)
{
    while (n) {
        uint8_t *dst;
        uint32_t k = ring_buf_put_claim(&live_rb, &dst, n);
        memcpy(dst, d, k);
        d = (const uint8_t *) d + k;
        n -= k;
    }
}

static void live(const uint8_t *b, uint32_t len, uint32_t offset)
{
    struct bt_conn *conn = get_current_connection();
    if (!conn || !bt_gatt_is_subscribed(conn, LIVE_ATTR, BT_GATT_CCC_NOTIFY)) {
        return;
    }
    if (ring_buf_space_get(&live_rb) < sizeof(struct live_rec) + len) {
        live_lost++;
        live_lost_bytes += len;
        return;
    }
    struct live_rec h = {cur_file(), offset, len};
    live_claim(&h, sizeof(h));
    live_claim(b, len);
    ring_buf_put_finish(&live_rb, sizeof(h) + len);
    k_sem_give(&live_sem);
    if (live_lost) {
        lr_log(LR_LIVE_LOST, 0, live_lost, live_lost_bytes);
        live_lost = live_lost_bytes = 0;
    }
}

/* The queued records to the phone that follows, split to the MTU. A record for a link that went is dropped, and a
 * notify that fails (the link going) drops the rest of its record: a gap the phone fills with READ. */
static void live_send(void)
{
    static uint8_t p[8 + 512];
    struct live_rec h;
    while (ring_buf_get(&live_rb, (uint8_t *) &h, sizeof(h)) == sizeof(h)) {
        struct bt_conn *conn = get_current_connection();
        if (!conn || !bt_gatt_is_subscribed(conn, LIVE_ATTR, BT_GATT_CCC_NOTIFY)) {
            ring_buf_get(&live_rb, NULL, h.len);
            continue;
        }
        conn = bt_conn_ref(conn);
        uint16_t room = MIN(bt_gatt_get_mtu(conn) - 3 - 8, sizeof(p) - 8);
        bool ok = true;
        for (uint32_t i = 0; i < h.len; i += room) {
            uint16_t m = MIN(room, h.len - i);
            ring_buf_get(&live_rb, p + 8, m);  // taken either way: the queue goes on with the next record
            sys_put_le32(h.file, p);
            sys_put_le32(h.offset + i, p + 4);
            ok = ok && !bt_gatt_notify(conn, LIVE_ATTR, p, 8 + m);
        }
        bt_conn_unref(conn);
    }
}

static void live_thread(void)
{
    for (;;) {
        k_sem_take(&live_sem, K_FOREVER);
        live_send();
    }
}
K_THREAD_DEFINE(live_tid, 2048, live_thread, NULL, NULL, NULL, K_PRIO_PREEMPT(8), 0, 0);  // between the pusher (7) and the store (9): LIVE before READ's DATA

/* Notify until it fits in the TX buffers; false once the link is gone. */
static bool notify(struct bt_conn *conn, const struct bt_gatt_attr *attr, const void *data, uint16_t len)
{
    for (;;) {
        int err = bt_gatt_notify(conn, attr, data, len);
        if (err != -ENOMEM) {
            return !err;
        }
        k_sleep(K_MSEC(1));
    }
}

static void reply(struct bt_conn *conn, uint8_t op, uint32_t a, uint32_t b, uint8_t blen)
{
    uint8_t v[9] = {op};
    sys_put_le32(a, v + 1);
    sys_put_le32(b, v + 5);
    notify(conn, CTRL_ATTR, v, 5 + blen);
}

static void list(struct bt_conn *conn)
{
    // ponytail: the first 64 files per LIST; a phone deletes what it took and lists again
    static uint32_t files[64][2];
    int n = 0;
    struct fs_dir_t d;
    struct fs_dirent e;
    fs_dir_t_init(&d);
    k_mutex_lock(&write_sdcard_mutex, K_FOREVER);
    int err = fs_opendir(&d, DIR), opened = !err;
    while (!err && n < ARRAY_SIZE(files) && !(err = fs_readdir(&d, &e)) && e.name[0]) {
        uint32_t file = strtoul(e.name, NULL, 16);
        if (e.type == FS_DIR_ENTRY_FILE && strlen(e.name) == 12) {
            files[n][0] = file;
            files[n++][1] = e.size;
        }
    }
    if (opened) {
        fs_closedir(&d);
    }
    k_mutex_unlock(&write_sdcard_mutex);
    for (int i = 0; i < n; i++) {
        reply(conn, 0x81, files[i][0], files[i][1], 4);
    }
    uint8_t end[6] = {0x80, (uint8_t) err};
    sys_put_le32(cur_file(), end + 2);  // the open segment, listed too: the one the stream goes on in
    notify(conn, CTRL_ATTR, end, sizeof(end));
}

static uint8_t chunk[CHUNK];
static uint8_t pkt[4 + 512];

/* LOG: records seq.. are at pkt + 4 already (lr_log_read put them there) */
static bool send_log(uint32_t seq, uint16_t n, void *conn)
{
    sys_put_le32(seq, pkt);
    return notify(conn, DATA_ATTR, pkt, 4 + n * 16);
}

/* Files go at a 15 ms interval, the shortest iOS grants a peripheral; a shorter one the phone already has (an Android
 * app asks for its own) is kept. The slow one (30-45 ms), also the GAP preferred parameters Android applies a second
 * into a link, again once no READ came for 5 s: a catch-up is READ after READ. Both within Apple's rules for a
 * peripheral's request (min a multiple of 15 ms, timeout 2-6 s), which the old 7.5-15 ms and its 0.42 s timeout broke. */
#define LINK_FAST BT_LE_CONN_PARAM(12, 12, 0, 400)
#define LINK_SLOW                                                                                                      \
    BT_LE_CONN_PARAM(CONFIG_BT_PERIPHERAL_PREF_MIN_INT, CONFIG_BT_PERIPHERAL_PREF_MAX_INT,                             \
                     CONFIG_BT_PERIPHERAL_PREF_LATENCY, CONFIG_BT_PERIPHERAL_PREF_TIMEOUT)

/* The fast interval, if the link is slower: at a READ's start and on while it goes (the phone may change it, as
 * Android does with the preferred parameters); at most every 5 s, a request takes a while to land */
static void link_fast(struct bt_conn *conn)
{
    static int64_t asked;
    struct bt_conn_info info;
    if (k_uptime_get() - asked > 5000 && !bt_conn_get_info(conn, &info) && info.le.interval > 12) {
        asked = k_uptime_get();
        bt_conn_le_param_update(conn, LINK_FAST);
    }
}

static void link_slow(struct k_work *work)
{
    struct bt_conn *conn = get_current_connection();
    if (conn) {
        bt_conn_le_param_update(conn, LINK_SLOW);
    }
}
static K_WORK_DELAYABLE_DEFINE(slow_work, link_slow);

/* Send one file from offset; a newer command (STOP, another READ) ends it early. Where the time went: READ and
 * READ_TIME events (lr_log.h). */
static void send_file(struct bt_conn *conn, uint32_t file, uint32_t off)
{
    char p[32];
    struct fs_file_t f;
    fs_file_t_init(&f);
    int64_t t0 = k_uptime_get(), t;
    uint32_t from = off, card_ms = 0, send_ms = 0;
    k_work_cancel_delayable(&slow_work);
    link_fast(conn);
    uint16_t payload = MIN(bt_gatt_get_mtu(conn) - 3 - 4, sizeof(pkt) - 4);
    bool ok = true;
    uint32_t end = UINT32_MAX;
    k_mutex_lock(&write_sdcard_mutex, K_FOREVER);
    if (file == cur_file()) {  // the open segment: its finished records onto the card, and no further (LIVE has the rest)
        write_out();
        end = seg_size;
    }
    // opened once and read on: an open per chunk looked the name up again and its seek walked the cluster chain from
    // the file's start, slower the further in
    t = k_uptime_get();
    int n = fs_open(&f, file_path(p, file), FS_O_READ);
    bool opened = !n;
    n = n ?: fs_seek(&f, off, FS_SEEK_SET);
    card_ms += k_uptime_get() - t;
    k_mutex_unlock(&write_sdcard_mutex);
    while (!n && ok && off < end && !k_msgq_num_used_get(&cmds)) {
        k_mutex_lock(&write_sdcard_mutex, K_FOREVER);
        t = k_uptime_get();
        int got = fs_read(&f, chunk, MIN(CHUNK, end - off));
        card_ms += k_uptime_get() - t;
        k_mutex_unlock(&write_sdcard_mutex);
        if (got <= 0) {
            break;  // the end (or a read error: the phone sees where it stopped)
        }
        link_fast(conn);
        t = k_uptime_get();
        for (int i = 0; ok && i < got; i += payload) {
            uint16_t m = MIN(payload, got - i);
            sys_put_le32(off + i, pkt);
            memcpy(pkt + 4, chunk + i, m);
            ok = notify(conn, DATA_ATTR, pkt, 4 + m);
        }
        send_ms += k_uptime_get() - t;
        off += got;
    }
    if (opened) {
        k_mutex_lock(&write_sdcard_mutex, K_FOREVER);
        fs_close(&f);
        k_mutex_unlock(&write_sdcard_mutex);
    }
    if (ok) {
        reply(conn, 0x82, file, off, 4);
    }
    if (off > from) {
        struct bt_conn_info info = {0};
        bt_conn_get_info(conn, &info);
        lr_log(LR_READ, MIN(info.le.interval, 0xFF), off - from,
               MIN(k_uptime_get() - t0, 0x3FFFFF) | (uint32_t) payload << 22);
        lr_log(LR_READ_TIME, 0, card_ms, send_ms);
    }
    k_work_reschedule(&slow_work, K_SECONDS(5));
}

struct forget {
    struct bt_conn *keep;
    int *n;
};

/* Every bond but the asking phone's: a lost or replaced phone can no longer connect */
static void forget_bond(const struct bt_bond_info *info, void *arg)
{
    struct forget *f = arg;
    if (!bt_addr_le_eq(&info->addr, bt_conn_get_dst(f->keep))) {
        bt_unpair(BT_ID_DEFAULT, &info->addr);
        (*f->n)++;
    }
}

extern volatile bool usb_disk_lent;  // usb.c: the card reader shows the card
extern bool usb_charge;

/* The card to the computer (USB disk) and back. Lent, the card is unmounted and nothing records to it (-EBUSY); back,
 * it is mounted again (the ProMicro powers it off and on first) and the card goes on in a new segment. The stream
 * itself never stops. */
static int lend(bool on)
{
    if (on == usb_disk_lent) {
        return 0;
    }
    if (on && (!usb_charge || store_err)) {
        return usb_charge ? store_err : -ENOTCONN;  // not on a computer, or no card to lend
    }
    k_mutex_lock(&write_sdcard_mutex, K_FOREVER);
    if (on) {
        flush();
        close_segment();
        unmount_sd_card();
        store_err = -EBUSY;
        usb_disk_lent = true;
        LOG_INF("card lent to USB");
        lr_log(LR_USB_DISK, 1, 0, 0);
        k_mutex_unlock(&write_sdcard_mutex);
        usb_reattach();
        return 0;
    } else {
        usb_disk_lent = false;
        int err = mount_sd_card();
        store_err = err;
        if (!err) {
            restart_segment();
            count_free();
        }
        LOG_INF("card back from USB: %d", err);
        lr_log(LR_USB_DISK, 0, err, 0);
    }
    k_mutex_unlock(&write_sdcard_mutex);
    return 0;
}

static int card_init(int (*mount)(void), uint16_t *counted);

/* A card that failed (a write, the mount, none at boot) is tried again once a minute, one mount try, the SD mutex
 * not held meanwhile (the stream goes on). Back, it goes on in a new segment, as after a lend; a card that was not
 * there at boot counts this boot then and starts at its segment 0. Lent (-EBUSY) and full (-ENOSPC: back when a phone
 * deletes) are not failures. */
static void retry_card(void)
{
    if (!store_err || store_err == -EBUSY || store_err == -ENOSPC) {
        return;
    }
    k_mutex_lock(&write_sdcard_mutex, K_FOREVER);
    close_segment();
    uint16_t was = boot;
    k_mutex_unlock(&write_sdcard_mutex);
    int err;
    unmount_sd_card();  // whatever state the failure left it in
    if (was) {
        err = mount_sd_card_once();
    } else {
        err = card_init(mount_sd_card_once, &was);  // counts the boot
    }
    k_mutex_lock(&write_sdcard_mutex, K_FOREVER);
    if (!err) {
        flush();  // the stream's last records go with the file they are in (not on the card)
        if (!boot) {
            boot = was;
            seg = seg_size = 0;
            file_path(path, cur_file());
            lr_log(LR_SEGMENT, 0, cur_file(), 0);
        } else {
            restart_segment();
        }
        store_err = 0;
        count_free();
        LOG_INF("card back: %08X", cur_file());
    } else if (was) {
        store_err = err;
    }
    k_mutex_unlock(&write_sdcard_mutex);
}

static void store_thread(void)
{
    struct cmd c;
    int64_t tried = k_uptime_get();
    for (;;) {
        int got = k_msgq_get(&cmds, &c, K_SECONDS(60));
        if (k_uptime_get() - tried >= 60000) {
            tried = k_uptime_get();
            retry_card();
        }
        if (got) {
            continue;
        }
        if (c.op == UNLEND) {
            lend(false);
            continue;
        }
        struct bt_conn *conn = get_current_connection();
        if (c.op == FOLLOW) {
            k_mutex_lock(&write_sdcard_mutex, K_FOREVER);
            next_segment();  // nothing new if the segment is empty
            k_mutex_unlock(&write_sdcard_mutex);
            continue;
        }
        if (c.op == SYNC) {  // into the stream, where it is: no new segment
            k_mutex_lock(&write_sdcard_mutex, K_FOREVER);
            put_sync(c.frame, c.epoch);
            k_mutex_unlock(&write_sdcard_mutex);
            LOG_INF("sync: frame %u = %llu", c.frame, c.epoch);
            continue;
        }
        if (!conn) {
            continue;
        }
        conn = bt_conn_ref(conn);
        if (c.op == LIST) {
            // the open segment is listed with what the stream has finished: written out first
            k_mutex_lock(&write_sdcard_mutex, K_FOREVER);
            write_out();
            count_free();
            k_mutex_unlock(&write_sdcard_mutex);
            list(conn);
        } else if (c.op == READ) {
            send_file(conn, c.file, c.offset);
        } else if (c.op == USB_DISK) {
            int err = lend(c.file);
            reply(conn, 0x86, c.file, (uint8_t) err, 1);
        } else if (c.op == FORGET && c.file) {  // unbinding: the reply first, the link goes with this phone's bond
            reply(conn, 0x85, lr_bond_count(), 0, 0);
            bt_unpair(BT_ID_DEFAULT, BT_ADDR_LE_ANY);
        } else if (c.op == FORGET) {
            int n = 0;
            bt_foreach_bond(BT_ID_DEFAULT, forget_bond, &(struct forget){conn, &n});
            lr_log(LR_FORGET, 0, n, 0);
            reply(conn, 0x85, n, 0, 0);
        } else if (c.op == LOG) {
            uint16_t max = MIN(bt_gatt_get_mtu(conn) - 3 - 4, sizeof(pkt) - 4) / 16;
            reply(conn, 0x87, lr_log_read(c.file, pkt + 4, max, send_log, conn), 0, 0);
        } else if (c.op == DELETE) {
            char p[32];
            int err = -EBUSY;
            if (c.file != cur_file()) {
                k_mutex_lock(&write_sdcard_mutex, K_FOREVER);
                err = fs_unlink(file_path(p, c.file));
                count_free();
                k_mutex_unlock(&write_sdcard_mutex);
            }
            reply(conn, 0x84, c.file, (uint8_t) err, 1);
        }
        bt_conn_unref(conn);
    }
}
K_THREAD_STACK_DEFINE(store_stack, 3072);  // 2048 left 752 B unused (event log STACKS, 10-04)
struct k_thread store_thread_data;  // lr_log.c watches its stack

void lr_store_shutdown(void)
{
    k_mutex_lock(&write_sdcard_mutex, K_FOREVER);  // kept: nothing writes the card after this
    if (!store_err) {
        flush();
        close_segment();
        unmount_sd_card();
    }
}

void lr_store_lend(bool on)
{
    if (!on) {
        struct cmd c = {.op = UNLEND};
        k_msgq_put(&cmds, &c, K_NO_WAIT);
    }
}

/* Mount the SD card, count this boot, pick its first segment; start the command thread, card or not (the stream, its
 * 'T' records and LIVE go on without one). */
int lr_store_init(void)
{
    resetreas = NRF_POWER->RESETREAS;
    NRF_POWER->RESETREAS = resetreas;  // write-1-to-clear: the next boot sees only its own
    if (fault[0] == 0xFA017) {
        last_fault[0] = fault[1];
        last_fault[1] = fault[2];
        fault[0] = 0;
        lr_log(LR_FAULT, 0, last_fault[0], last_fault[1]);
    }
    int err = card_init(mount_sd_card, &boot);
    store_err = err;
    if (err) {
        seg = sys_rand32_get();  // boot 0 has no count: a random first segment keeps this boot's files apart from another's
    }
    file_path(path, cur_file());
    k_thread_create(&store_thread_data, store_stack, K_THREAD_STACK_SIZEOF(store_stack),
                    (k_thread_entry_t) store_thread, NULL, NULL, NULL,
                    K_PRIO_PREEMPT(9), 0, K_NO_WAIT);  // below the pusher (7): sending a file never starves recording
    return err;
}

/* Mount, set the card up, count this boot into *counted. The caller sets store_err (and boot, under the SD mutex). */
static int card_init(int (*mount)(void), uint16_t *counted)
{
    struct fs_file_t f;
    fs_file_t_init(&f);
    int err = mount();
    if (err) {
        return err;
    }
    // a computer the card is lent to (USB disk) leaves it alone: no Spotlight index, no fseventsd log
    struct fs_file_t m;
    fs_file_t_init(&m);
    if (!fs_open(&m, "/SD:/.metadata_never_index", FS_O_CREATE | FS_O_WRITE)) {
        fs_close(&m);
    }
    fs_mkdir("/SD:/.fseventsd");
    fs_file_t_init(&m);
    if (!fs_open(&m, "/SD:/.fseventsd/no_log", FS_O_CREATE | FS_O_WRITE)) {
        fs_close(&m);
    }
    err = fs_mkdir(DIR);
    if (err && err != -EEXIST) {
        LOG_ERR("mkdir " DIR ": %d", err);
        return err;
    }
    err = fs_open(&f, DIR "/BOOT", FS_O_CREATE | FS_O_RDWR);
    if (err) {
        return err;
    }
    uint8_t b[2] = {0};
    fs_read(&f, b, 2);
    *counted = sys_get_le16(b) + 1;
    sys_put_le16(*counted, b);
    fs_seek(&f, 0, FS_SEEK_SET);
    fs_write(&f, b, 2);
    fs_close(&f);
    count_free();
    LOG_INF("boot %u, %u of %u KB free", *counted, free_kb, total_kb);
    lr_log(LR_STORE, 0, *counted, free_kb);
    return 0;
}
