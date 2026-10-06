// Host check of the record stream in src/lr_store.c: what a following phone gets (LIVE) and what the card gets are the
// same bytes at the same file and offset, through batches, 'T' records, frame gaps and a card that stops taking writes;
// the timing events (SD_WRITES, READ, READ_TIME) on a fake clock; LIVE behind a stalled link (#133).
//   d=$(mktemp -d) && mkdir -p $d/zephyr/{bluetooth,fs,sys,logging,drivers,random} && for h in bluetooth/conn \
//   bluetooth/gatt bluetooth/uuid fs/fs kernel logging/log sys/byteorder sys/ring_buffer drivers/sensor random/random; do
//   touch $d/zephyr/$h.h; done && cc -O1 -w -I$d -o $d/store_check test/store_check.c && $d/store_check
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>

// --- the Zephyr API lr_store.c uses, as much as the stream needs
#define CONFIG_LR_STORE_RESERVE_KB 1024
#define CONFIG_LR_STORE_BATCH 4096
#define LOG_MODULE_REGISTER(...)
#define LOG_INF(...)
#define LOG_ERR(...)
#define __noinit
#define MIN(a, b) ((a) < (b) ? (a) : (b))
#define MAX(a, b) ((a) > (b) ? (a) : (b))
#define DIV_ROUND_UP(n, d) (((n) + (d) - 1) / (d))
#define ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))
#define K_FOREVER 0
#define K_NO_WAIT 0
#define K_MSEC(x) (x)
#define K_SECONDS(x) (x)
#define K_PRIO_PREEMPT(x) (x)
#define K_MSGQ_DEFINE(name, ...) static int name
#define K_WORK_DELAYABLE_DEFINE(name, fn) int name  // after "static"
#define K_THREAD_STACK_DEFINE(name, n) static char name[n]
#define K_THREAD_STACK_SIZEOF(s) sizeof(s)
#define BT_GATT_SERVICE_DEFINE(name, ...) static struct bt_gatt_attr name##_attrs[12]; static struct { struct bt_gatt_attr *attrs; } name = {name##_attrs}
#define BT_GATT_ERR(x) (-(x))
#define BT_ATT_ERR_INVALID_ATTRIBUTE_LEN 13
#define BT_GATT_CCC_NOTIFY 1
#define BT_ID_DEFAULT 0
#define BT_ADDR_LE_ANY ((const bt_addr_le_t *) 0)
#define BT_LE_CONN_PARAM(...) 0
#define BT_CONN_LE_PHY_PARAM_2M 0
#define FS_O_CREATE 1
#define FS_O_WRITE 2
#define FS_O_APPEND 4
#define FS_O_READ 8
#define FS_O_RDWR 10
#define FS_SEEK_SET 0
#define FS_DIR_ENTRY_FILE 0
typedef void (*k_thread_entry_t)(void *, void *, void *);
struct k_mutex { int x; };
struct sensor_value { int v; };
struct k_thread { int x; };
struct bt_conn { int x; };
struct bt_conn_info { struct { uint16_t interval; } le; };
struct k_work { int x; };
struct bt_gatt_attr { int x; };
typedef struct { uint8_t a; } bt_addr_le_t;
struct bt_bond_info { bt_addr_le_t addr; };
typedef struct { struct { uint32_t pc; } basic; } z_arch_esf_t;
struct fs_file_t { int f; uint32_t pos; };
struct fs_dir_t { int x; };
struct fs_dirent { int type; char name[16]; uint32_t size; };
struct fs_statvfs { unsigned long f_bfree, f_frsize, f_blocks; };
static struct { uint32_t RESETREAS; } power;
#define NRF_POWER (&power)
static void NVIC_SystemReset(void) {}
static void sys_put_le16(uint16_t v, uint8_t *b) { b[0] = v; b[1] = v >> 8; }
static void sys_put_le32(uint32_t v, uint8_t *b) { sys_put_le16(v, b); sys_put_le16(v >> 16, b + 2); }
static void sys_put_le64(uint64_t v, uint8_t *b) { sys_put_le32(v, b); sys_put_le32(v >> 32, b + 4); }
static uint16_t sys_get_le16(const uint8_t *b) { return b[0] | b[1] << 8; }
static uint32_t sys_get_le32(const uint8_t *b) { return sys_get_le16(b) | (uint32_t) sys_get_le16(b + 2) << 16; }
static uint64_t sys_get_le64(const uint8_t *b) { return sys_get_le32(b) | (uint64_t) sys_get_le32(b + 4) << 32; }
static int k_mutex_lock(struct k_mutex *m, int t) { return 0; }
static int k_mutex_unlock(struct k_mutex *m) { return 0; }
static int k_msgq_put(int *q, void *c, int t) { return 0; }
static int k_msgq_get(int *q, void *c, int t) { return -1; }
static int k_msgq_num_used_get(int *q) { return 0; }
static void k_sleep(int t) {}
static uint32_t sys_rand32_get(void) { return 0x1234; }
static int64_t now_ms;  // the clock: card writes, card reads and notifications take time
static int write_ms, read_ms, notify_ms;
static int64_t k_uptime_get(void) { return now_ms; }
static void k_work_cancel_delayable(int *w) {}
static void k_work_reschedule(int *w, int d) {}
static void k_thread_create(struct k_thread *t, char *s, size_t n, k_thread_entry_t e, void *a, void *b, void *c, int p, int o, int d) {}
static struct bt_conn the_conn;
struct bt_conn *get_current_connection(void) { return &the_conn; }
static struct bt_conn *bt_conn_ref(struct bt_conn *c) { return c; }
static void bt_conn_unref(struct bt_conn *c) {}
static int bt_conn_le_param_update(struct bt_conn *c, int p) { return 0; }
static int bt_conn_le_phy_update(struct bt_conn *c, int p) { return 0; }
static int bt_conn_get_info(struct bt_conn *c, struct bt_conn_info *i) { i->le.interval = 12; return 0; }
static const bt_addr_le_t *bt_conn_get_dst(struct bt_conn *c) { return 0; }
static bool bt_addr_le_eq(const bt_addr_le_t *a, const bt_addr_le_t *b) { return true; }
static int bt_unpair(int id, const bt_addr_le_t *a) { return 0; }
static void bt_foreach_bond(int id, void (*f)(const struct bt_bond_info *, void *), void *arg) {}
static uint16_t bt_gatt_get_mtu(struct bt_conn *c) { return 185; }
// LIVE's queue and its sending thread (#133): live_send() runs at each k_sem_give, unless the link is stalled
struct ring_buf { uint8_t *buf; uint32_t size, head, tail, claim; };  // read, committed, claimed: running counts
#define RING_BUF_DECLARE(name, n) static uint8_t name##_mem[n]; static struct ring_buf name = {name##_mem, n}
static uint32_t ring_buf_space_get(struct ring_buf *r) { return r->size - (r->claim - r->head); }
static uint32_t ring_buf_put_claim(struct ring_buf *r, uint8_t **d, uint32_t n)
{
    uint32_t at = r->claim % r->size;
    n = MIN(n, MIN(ring_buf_space_get(r), r->size - at));
    *d = r->buf + at;
    r->claim += n;
    return n;
}
static int ring_buf_put_finish(struct ring_buf *r, uint32_t n) { r->tail += n; assert(r->tail == r->claim); return 0; }
static uint32_t ring_buf_get(struct ring_buf *r, uint8_t *d, uint32_t n)
{
    n = MIN(n, r->tail - r->head);
    for (uint32_t i = 0; d && i < n; i++) {
        d[i] = r->buf[(r->head + i) % r->size];
    }
    r->head += n;
    return n;
}
#define __packed __attribute__((packed))
#define K_SEM_DEFINE(name, ...) int name  // after "static"
#define K_THREAD_DEFINE(...)
static bool stalled;  // the link: nothing goes out (LIVE's thread waits on a TX buffer)
static void live_send(void);
static void k_sem_give(int *s) { if (!stalled) live_send(); }
static int k_sem_take(int *s, int t) { return 0; }
static bool holes_ok;  // the phone gets LIVE with a gap (it READs that from the card)
static bool subscribed = true;
static bool bt_gatt_is_subscribed(struct bt_conn *c, const struct bt_gatt_attr *a, int v) { return subscribed; }
static ssize_t bt_gatt_attr_read(struct bt_conn *c, const struct bt_gatt_attr *a, void *b, uint16_t l, uint16_t o, const void *v, uint16_t n) { return n; }
int16_t usb_diag[4];
int lr_bond_count(void) { return 1; }
uint32_t lr_pause_left(void) { return 0; }
void lr_pause(uint32_t s) {}
void usb_reattach(void) {}
int mount_sd_card(void) { return 0; }
static int mounts;  // tries of the card again (retry_card)
int mount_sd_card_once(void) { mounts++; return 0; }
static struct { uint8_t code, x; uint32_t a, b; } logged[16];
static int n_logged;
void lr_log(uint8_t code, uint8_t x, uint32_t a, uint32_t b)
{
    if (n_logged < 16) {
        logged[n_logged].code = code, logged[n_logged].x = x, logged[n_logged].a = a, logged[n_logged++].b = b;
    }
}
uint32_t lr_log_read(uint32_t from, uint8_t *buf, uint16_t max, bool (*send)(uint32_t, uint16_t, void *), void *arg) { return 0; }
void unmount_sd_card(void) {}

// a phone following the stream: file -> bytes by offset
#define FILES 8
static uint8_t phone[FILES][1 << 16];
static uint32_t phone_len[FILES];
static const struct bt_gatt_attr *data_attr, *ctrl_attr;  // READ's: DATA offset:u32 bytes..., CTRL replies
static uint32_t data_next;                                // the offset the next DATA notification must have
static int bt_gatt_notify(struct bt_conn *c, const struct bt_gatt_attr *a, const void *data, uint16_t len)
{
    const uint8_t *p = data;
    assert(len <= bt_gatt_get_mtu(c) - 3);
    if (a == ctrl_attr) {
        return 0;
    }
    if (a == data_attr) {
        assert(sys_get_le32(p) == data_next);
        data_next += len - 4;
        now_ms += notify_ms;
        return 0;
    }
    uint32_t file = sys_get_le32(p) & 0xffff, off = sys_get_le32(p + 4);
    assert(file < FILES);
    assert(off == phone_len[file] || (holes_ok && off > phone_len[file]));  // no holes while the phone follows
    memcpy(phone[file] + off, p + 8, len - 8);
    phone_len[file] = off + len - 8;
    return 0;
}

// the card: /SD:/LR/0001SSSS.BIN -> bytes; BOOT
static uint8_t card[FILES][1 << 16];
static uint32_t card_len[FILES];
static int card_fail;  // writes fail with this
static int seg_of(const char *path) { return strstr(path, ".BIN") ? (int) (strtoul(strstr(path, ".BIN") - 4, 0, 16)) : -1; }
static int fs_open(struct fs_file_t *f, const char *path, int flags) { f->f = seg_of(path); f->pos = 0; return 0; }
static int fs_close(struct fs_file_t *f) { return 0; }
static ssize_t fs_write(struct fs_file_t *f, const void *d, size_t n)
{
    if (card_fail) {
        return card_fail;
    }
    if (f->f >= 0) {
        memcpy(card[f->f] + card_len[f->f], d, n);
        card_len[f->f] += n;
    }
    now_ms += write_ms;
    return n;
}
static ssize_t fs_read(struct fs_file_t *f, void *d, size_t n)
{
    n = MIN(n, card_len[f->f] - f->pos);
    memcpy(d, card[f->f] + f->pos, n);
    f->pos += n;
    now_ms += read_ms;
    return n;
}
static int fs_seek(struct fs_file_t *f, long o, int w) { f->pos = o; return 0; }
static int fs_sync(struct fs_file_t *f) { return -EIO; }  // as NCS 2.7 does
static void fs_file_t_init(struct fs_file_t *f) {}
static void fs_dir_t_init(struct fs_dir_t *d) {}
static int fs_opendir(struct fs_dir_t *d, const char *p) { return -ENOENT; }
static int fs_readdir(struct fs_dir_t *d, struct fs_dirent *e) { return -ENOENT; }
static int fs_closedir(struct fs_dir_t *d) { return 0; }
static int fs_mkdir(const char *p) { return 0; }
static int fs_unlink(const char *p) { return 0; }
static int fs_stat(const char *p, struct fs_dirent *e) { return -ENOENT; }
static int fs_statvfs(const char *p, struct fs_statvfs *v) { v->f_bfree = v->f_blocks = 1 << 20; v->f_frsize = 4096; return 0; }

struct k_mutex write_sdcard_mutex;
uint32_t transport_frame_no;
#include "../src/lr_store.c"

static void frames(uint32_t from, uint32_t n)
{
    for (uint32_t i = from; i < from + n; i++) {
        uint8_t opus[40];
        memset(opus, (uint8_t) i, sizeof(opus));
        lr_store_frame(i, opus, sizeof(opus) - i % 7, false);  // VBR: sizes vary
    }
}

/* Parse a file's records: frames and 'T's, consecutive within each 'A', nothing left over. */
static void parse(const uint8_t *b, uint32_t n, int *a_frames, int *syncs)
{
    for (uint32_t i = 0; i < n;) {
        if (b[i] == 'A') {
            int c = b[i + 1];
            assert(c >= 1 && c <= REC_FRAMES);
            i += 6;
            while (c--) {
                i += 1 + b[i];
                ++*a_frames;
            }
        } else {
            assert(b[i] == 'T');
            i += 13;
            ++*syncs;
        }
        assert(i <= n);
    }
}

int main(void)
{
    assert(lr_store_init() == 0 && boot == 1 && store_err == 0);

    frames(0, 950);       // 9.5 s: several batches
    put_sync(950, 123);   // a phone's TIME
    frames(950, 50);
    frames(1100, 45);     // 100 frames lost on the way (a gap): a new record
    write_out();          // a LIST or READ mid-record: what is finished goes to the card, the open record stays
    assert(card_len[0] == phone_len[0] && rec_count == 5);
    frames(1145, 55);
    // the card trails the stream by less than a batch; READ (or a reset) writes the rest
    assert(card_len[0] < phone_len[0] && phone_len[0] - card_len[0] < BATCH + 6 + REC_FRAMES * 256);
    assert(memcmp(card[0], phone[0], card_len[0]) == 0);
    flush();
    assert(card_len[0] == phone_len[0] && memcmp(card[0], phone[0], card_len[0]) == 0 && seg_size == card_len[0]);
    int a = 0, t = 0;
    parse(card[0], card_len[0], &a, &t);
    assert(a == 1100 && t == 1);

    // the card stops taking writes: the stream goes on in the same file, the card's copy stays its start
    uint32_t before = card_len[0];
    card_fail = -EIO;
    frames(1200, 300);
    card_fail = 0;
    frames(1500, 200);
    flush();
    assert(store_err == -EIO && card_len[0] == before && phone_len[0] > before && seg == 0);
    assert(memcmp(card[0], phone[0], card_len[0]) == 0);

    // the card tried again (once a minute): back, in a new segment, the card and the stream the same again
    retry_card();
    assert(mounts == 1 && store_err == 0);
    frames(1700, 500);
    flush();
    assert(seg == 1 && card_len[1] == phone_len[1] && memcmp(card[1], phone[1], card_len[1]) == 0);
    a = t = 0;
    parse(phone[0], phone_len[0], &a, &t);
    parse(phone[1], phone_len[1], &a, &t);
    assert(a == 2100 && t == 1);  // every frame in the stream, 1200..1699 too though the card missed them

    // lent to USB: the stream goes on, the card does not; back, a new segment that is the stream's from its start
    store_err = -EBUSY;
    frames(2200, 343);  // ends with finished records still in RAM (not a whole batch)
    store_err = 0;
    assert(done > 0);
    restart_segment();  // as lend(false) does
    frames(2543, 290);
    flush();
    assert(seg == 2 && card_len[2] == phone_len[2] && memcmp(card[2], phone[2], card_len[2]) == 0);

    // the link stalls (#133): LIVE's queue fills and the records after it are left out of LIVE, not out of the card;
    // once one goes again, LIVE_LOST says how many; the phone READs the gap
    n_logged = 0;
    stalled = true;
    uint32_t from = seg_size;
    int a0 = 0, s0 = 0;
    parse(card[2], from, &a0, &s0);  // the segment so far
    frames(2833, 400);  // 4 s: more than the queue holds
    assert(live_lost > 10 && live_lost_bytes > 2000 && phone_len[2] < from + 1);
    uint32_t lost = live_lost, lost_bytes = live_lost_bytes;
    stalled = false;
    holes_ok = true;
    live_send();  // the link is back: what the queue holds goes out
    frames(3233, 100);
    flush();
    assert(n_logged == 1 && logged[0].code == LR_LIVE_LOST && logged[0].a == lost && logged[0].b == lost_bytes);
    assert(live_lost == 0 && card_len[2] == phone_len[2] && seg == 2);  // the card has it all, the phone up to the end
    assert(memcmp(card[2], phone[2], from) == 0);
    a = t = 0;
    parse(card[2], card_len[2], &a, &t);
    assert(a == a0 + 400 + 100);  // every frame on the card
    holes_ok = false;

    // nobody follows: the card alone, same records
    subscribed = false;
    frames(3333, 100);
    flush();
    assert(card_len[2] > phone_len[2]);

    // card appends over 10 minutes, one SD_WRITES: x over 200 ms, a total | slowest << 20, b <20 | 20-49 << 10 | 50-200 << 20
    memset(&wr, 0, sizeof(wr));
    wr.start = now_ms = 1000;
    n_logged = 0;
    int took[] = {5, 5, 19, 20, 49, 50, 200, 250, 1001};
    for (int i = 0; i < ARRAY_SIZE(took); i++) {
        write_ms = took[i];
        assert(append("x", 1) == 0);
    }
    assert(n_logged == 1 && logged[0].code == LR_SLOW && logged[0].a == 1001);  // over 1000 ms only
    now_ms = 1000 + 10 * 60 * 1000;
    write_ms = 3;
    assert(append("x", 1) == 0);
    assert(n_logged == 2 && logged[1].code == LR_SD_WRITES && logged[1].x == 2);
    assert(logged[1].a == (1602 | 1001 << 20) && logged[1].b == (4 | 2 << 10 | 2 << 20));
    assert(wr.start == now_ms && wr.ms == 0);
    write_ms = 0;

    // a READ of a finished file from offset 100: READ (interval, bytes, ms | payload << 22) and READ_TIME (card, notify)
    data_attr = DATA_ATTR;
    ctrl_attr = CTRL_ATTR;
    n_logged = 0;
    read_ms = 7, notify_ms = 2;
    data_next = 100;
    int64_t t0 = now_ms;
    send_file(&the_conn, 1 << 16, 100);
    uint32_t bytes = card_len[0] - 100, payload = 185 - 3 - 4;
    uint32_t reads = (bytes + CHUNK - 1) / CHUNK + 1, sends = 0;  // the last read finds the end
    for (uint32_t off = 0; off < bytes; off += CHUNK) {
        sends += (MIN(CHUNK, bytes - off) + payload - 1) / payload;
    }
    assert(data_next == card_len[0] && n_logged == 2);
    assert(logged[0].code == LR_READ && logged[0].x == 12 && logged[0].a == bytes);
    assert(logged[0].b == ((now_ms - t0) | payload << 22) && now_ms - t0 == reads * 7 + sends * 2);
    assert(logged[1].code == LR_READ_TIME && logged[1].a == reads * 7 && logged[1].b == sends * 2);
    send_file(&the_conn, 1 << 16, card_len[0]);  // nothing to send: no event
    assert(n_logged == 2);
    printf("store_check ok: %u + %u + %u B on the card\n", card_len[0], card_len[1], card_len[2]);
    return 0;
}
