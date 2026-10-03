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
#include <zephyr/sys/byteorder.h>

#include "sdcard.h"
#include "transport.h"

LOG_MODULE_REGISTER(lr_store, CONFIG_LOG_DEFAULT_LEVEL);

extern struct k_mutex write_sdcard_mutex;  // transport.c: one SPI bus, one writer at a time
extern uint32_t transport_frame_no;        // transport.c: frames the codec has made since boot

#define DIR "/SD:/LR"
#define REC_FRAMES 100       // one record a second
#define SEG_MAX (16u << 20)  // a long offline stretch still ends in files a phone can take one by one
#define CHUNK 4096           // SD read size while sending a file
#define RESERVE_KB CONFIG_LR_STORE_RESERVE_KB  // recording stops (-ENOSPC) below this much free space, resumes at twice it

static uint16_t boot;
static int16_t store_err = -ENODEV;  // until lr_store_init succeeds: no card, or it failed
static uint16_t seg;
static uint32_t seg_size;
static char path[32];
static uint32_t free_kb, total_kb;  // the card, as of the last statvfs, less what was written since

static uint8_t rec[4096];
static uint32_t rec_len, rec_next;  // bytes in rec; the frame that may extend it
static uint8_t rec_count;

static const char *file_path(char *buf, uint32_t file)
{
    snprintf(buf, 32, DIR "/%08X.BIN", file);
    return buf;
}

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
        LOG_INF("card has room again: %u KB", free_kb);
    }
}

static uint32_t dropped;     // frames lost on the way to the card (gaps in the frame numbers it got)
static uint16_t append_max;  // the slowest append so far, ms

static int append(const void *data, uint32_t len)
{
    int64_t t0 = k_uptime_get();
    if (free_kb < RESERVE_KB) {
        if (store_err != -ENOSPC) {
            LOG_ERR("card full (%u KB free): not recording", free_kb);
        }
        store_err = -ENOSPC;
        return -ENOSPC;
    }
    struct fs_file_t f;
    fs_file_t_init(&f);
    int err = fs_open(&f, path, FS_O_CREATE | FS_O_WRITE | FS_O_APPEND);
    if (err) {
        return err;
    }
    int n = fs_write(&f, data, len);
    fs_close(&f);
    append_max = MAX(append_max, (uint16_t) MIN(k_uptime_get() - t0, UINT16_MAX));
    if (n > 0) {
        seg_size += n;
        free_kb -= MIN(free_kb, DIV_ROUND_UP(n, 1024));
    }
    return n == len ? 0 : -EIO;
}

static void flush(void)
{
    if (rec_count) {
        rec[1] = rec_count;
        if (append(rec, rec_len)) {
            LOG_ERR("SD write failed, %u frames lost", rec_count);
        }
    }
    rec_len = rec_count = 0;
}

static void next_segment(void)
{
    flush();
    if (seg_size) {  // an empty segment stays the open one
        seg++;
        seg_size = 0;
    }
    file_path(path, (uint32_t) boot << 16 | seg);
}

void lr_store_frame(uint32_t frame_no, const uint8_t *data, uint16_t len)
{
    if (len > 255 || (store_err && store_err != -ENOSPC)) {
        return;  // no card; a full one still takes the frames to the record, which append() then drops
    }
    if (rec_count && frame_no > rec_next) {
        dropped += frame_no - rec_next;
    }
    if (rec_count && (frame_no != rec_next || rec_count == REC_FRAMES || rec_len + 1 + len > sizeof(rec))) {
        flush();
    }
    if (seg_size >= SEG_MAX) {
        next_segment();
    }
    if (!rec_count) {
        rec[0] = 'A';
        sys_put_le32(frame_no, rec + 2);
        rec_len = 6;
    }
    rec[rec_len++] = len;
    memcpy(rec + rec_len, data, len);
    rec_len += len;
    rec_count++;
    rec_next = frame_no + 1;
}

//
// Commands, handled on the store's own thread: SD access and notifications may block, the BT RX thread must not
//
enum { LIST = 1, READ = 2, STOP = 3, DELETE = 4, FORGET = 5, USB_DISK = 6, SYNC = 0x10, ROTATE = 0x11, UNLEND = 0x12 };
struct cmd {
    uint8_t op;
    uint32_t file, offset;
    uint64_t epoch;
    uint32_t frame;
};
K_MSGQ_DEFINE(cmds, sizeof(struct cmd), 8, 4);

static ssize_t time_write(struct bt_conn *conn, const struct bt_gatt_attr *attr, const void *buf, uint16_t len,
                          uint16_t offset, uint8_t flags)
{
    if (len != 8) {
        return BT_GATT_ERR(BT_ATT_ERR_INVALID_ATTRIBUTE_LEN);
    }
    struct cmd c = {.op = SYNC, .epoch = sys_get_le64(buf), .frame = transport_frame_no};
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
    uint8_t v[42];
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
    if (c.op == USB_DISK && len >= 2) {
        c.file = b[1];  // on (1) / off (0)
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
                       BT_GATT_CCC(NULL, BT_GATT_PERM_READ | BT_GATT_PERM_WRITE_ENCRYPT), );
#define CTRL_ATTR (&lr_svc.attrs[4])
#define DATA_ATTR (&lr_svc.attrs[7])

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
    // ponytail: the first 64 closed files per LIST; a phone deletes what it took and lists again
    static uint32_t files[64][2];
    int n = 0;
    struct fs_dir_t d;
    struct fs_dirent e;
    fs_dir_t_init(&d);
    k_mutex_lock(&write_sdcard_mutex, K_FOREVER);
    int err = fs_opendir(&d, DIR), opened = !err;
    while (!err && n < ARRAY_SIZE(files) && !(err = fs_readdir(&d, &e)) && e.name[0]) {
        uint32_t file = strtoul(e.name, NULL, 16);
        if (e.type == FS_DIR_ENTRY_FILE && strlen(e.name) == 12 && file != ((uint32_t) boot << 16 | seg)) {
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
    uint8_t end[2] = {0x80, (uint8_t) err};
    notify(conn, CTRL_ATTR, end, 2);
}

static uint8_t chunk[CHUNK];
static uint8_t pkt[4 + 512];

/* Send one file from offset; a newer command (STOP, another READ) ends it early. */
static void send_file(struct bt_conn *conn, uint32_t file, uint32_t off)
{
    char p[32];
    struct fs_file_t f;
    fs_file_t_init(&f);
    // a short interval and 2M PHY while a file goes: the default 30-50 ms interval moves ~17 KB/s
    bt_conn_le_param_update(conn, BT_LE_CONN_PARAM(6, 12, 0, 400));
    bt_conn_le_phy_update(conn, BT_CONN_LE_PHY_PARAM_2M);
    uint16_t payload = MIN(bt_gatt_get_mtu(conn) - 3 - 4, sizeof(pkt) - 4);
    bool ok = true;
    while (ok && !k_msgq_num_used_get(&cmds)) {
        k_mutex_lock(&write_sdcard_mutex, K_FOREVER);
        int n = fs_open(&f, file_path(p, file), FS_O_READ);
        if (!n) {
            n = fs_seek(&f, off, FS_SEEK_SET) ?: fs_read(&f, chunk, CHUNK);
            fs_close(&f);
        }
        k_mutex_unlock(&write_sdcard_mutex);
        if (n <= 0) {
            break;  // the end (or a read error: the phone sees where it stopped)
        }
        for (int i = 0; ok && i < n; i += payload) {
            uint16_t m = MIN(payload, n - i);
            sys_put_le32(off + i, pkt);
            memcpy(pkt + 4, chunk + i, m);
            ok = notify(conn, DATA_ATTR, pkt, 4 + m);
        }
        off += n;
    }
    if (ok) {
        reply(conn, 0x82, file, off, 4);
    }
    bt_conn_le_param_update(conn, BT_LE_CONN_PARAM(24, 40, 0, 42));
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
 * it is mounted again (the ProMicro powers it off and on first) and recording goes on in a new segment. */
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
        unmount_sd_card();
        store_err = -EBUSY;
        usb_disk_lent = true;
        LOG_INF("card lent to USB");
    } else {
        usb_disk_lent = false;
        int err = mount_sd_card();
        store_err = err;
        if (!err) {
            seg++;
            seg_size = 0;
            file_path(path, (uint32_t) boot << 16 | seg);
            count_free();
        }
        LOG_INF("card back from USB: %d", err);
    }
    k_mutex_unlock(&write_sdcard_mutex);
    return 0;
}

static void store_thread(void)
{
    struct cmd c;
    for (;;) {
        k_msgq_get(&cmds, &c, K_FOREVER);
        if (c.op == UNLEND) {
            lend(false);
            continue;
        }
        struct bt_conn *conn = get_current_connection();
        if (c.op == SYNC || c.op == ROTATE) {
            k_mutex_lock(&write_sdcard_mutex, K_FOREVER);
            next_segment();
            if (c.op == SYNC) {
                uint8_t t[13] = {'T'};
                sys_put_le32(c.frame, t + 1);
                sys_put_le64(c.epoch, t + 5);
                append(t, sizeof(t));
                LOG_INF("sync: frame %u = %llu", c.frame, c.epoch);
            }
            k_mutex_unlock(&write_sdcard_mutex);
            continue;
        }
        if (!conn) {
            continue;
        }
        conn = bt_conn_ref(conn);
        if (c.op == LIST) {
            // what was recorded since the connection (while the phone took the files, before it listens) is listed too
            k_mutex_lock(&write_sdcard_mutex, K_FOREVER);
            next_segment();
            count_free();
            k_mutex_unlock(&write_sdcard_mutex);
            list(conn);
        } else if (c.op == READ) {
            send_file(conn, c.file, c.offset);
        } else if (c.op == USB_DISK) {
            int err = lend(c.file);
            reply(conn, 0x86, c.file, (uint8_t) err, 1);
        } else if (c.op == FORGET) {
            int n = 0;
            bt_foreach_bond(BT_ID_DEFAULT, forget_bond, &(struct forget){conn, &n});
            reply(conn, 0x85, n, 0, 0);
        } else if (c.op == DELETE) {
            char p[32];
            int err = -EBUSY;
            if (c.file != ((uint32_t) boot << 16 | seg)) {
                k_mutex_lock(&write_sdcard_mutex, K_FOREVER);
                struct fs_dirent e;
                err = fs_unlink(file_path(p, c.file));
                // NCS 2.7's card_ioctl() lacks a break after DISK_IOCTL_CTRL_SYNC, so every FatFs sync reports
                // a disk error (-EIO) after the work is done: gone is what counts
                if (err == -EIO && fs_stat(p, &e) == -ENOENT) {
                    err = 0;
                }
                count_free();
                k_mutex_unlock(&write_sdcard_mutex);
            }
            reply(conn, 0x84, c.file, (uint8_t) err, 1);
        }
        bt_conn_unref(conn);
    }
}
K_THREAD_STACK_DEFINE(store_stack, 2048);
static struct k_thread store_thread_data;

void lr_store_shutdown(void)
{
    k_mutex_lock(&write_sdcard_mutex, K_FOREVER);  // kept: nothing writes the card after this
    if (!store_err) {
        flush();
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

void lr_store_rotate(void)
{
    struct cmd c = {.op = ROTATE};
    k_msgq_put(&cmds, &c, K_NO_WAIT);
}

/* Mount the SD card, count this boot, pick its first segment, start the command thread. */
int lr_store_init(void)
{
    resetreas = NRF_POWER->RESETREAS;
    NRF_POWER->RESETREAS = resetreas;  // write-1-to-clear: the next boot sees only its own
    if (fault[0] == 0xFA017) {
        last_fault[0] = fault[1];
        last_fault[1] = fault[2];
        fault[0] = 0;
    }
    struct fs_file_t f;
    fs_file_t_init(&f);
    int err = mount_sd_card();
    if (err) {
        return store_err = err;
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
        return store_err = err;
    }
    err = fs_open(&f, DIR "/BOOT", FS_O_CREATE | FS_O_RDWR);
    if (err) {
        return store_err = err;
    }
    uint8_t b[2] = {0};
    fs_read(&f, b, 2);
    boot = sys_get_le16(b) + 1;
    sys_put_le16(boot, b);
    fs_seek(&f, 0, FS_SEEK_SET);
    fs_write(&f, b, 2);
    fs_close(&f);
    file_path(path, (uint32_t) boot << 16);
    store_err = 0;
    count_free();
    LOG_INF("boot %u, %u of %u KB free", boot, free_kb, total_kb);
    k_thread_create(&store_thread_data, store_stack, K_THREAD_STACK_SIZEOF(store_stack),
                    (k_thread_entry_t) store_thread, NULL, NULL, NULL,
                    K_PRIO_PREEMPT(9), 0, K_NO_WAIT);  // below the pusher (7): sending a file never starves recording
    return 0;
}
