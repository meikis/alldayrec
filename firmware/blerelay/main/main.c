/* BLE relay on an ESP32-S3: the Mac (relay.py) drives it over one plain Bluetooth link, and it is the Bluetooth central
 * for one board under test: scan, connect, pair (LESC Just Works, bonds kept in NVS), read, write, notifications.
 * The Mac pairs with nothing (it cannot confirm a pairing while its screen is locked) and gives up no USB port.
 * Frames on CMD (Mac -> relay) and EVT (relay -> Mac, notifications) are an op byte and little-endian fields; every
 * op but HELLO/AUTH needs AUTH first: HMAC-SHA256(relay key, nonce) of this link's nonce (README.md). */
#include <string.h>
#include <sys/param.h>
#include "esp_log.h"
#include "esp_random.h"
#include "nvs_flash.h"
#include "mbedtls/md.h"
#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"
#include "host/ble_hs.h"
#include "host/util/util.h"
#include "services/gap/ble_svc_gap.h"
#include "services/gatt/ble_svc_gatt.h"
#include "relay_key.h"  // static const uint8_t relay_key[32], written by main/CMakeLists.txt

void ble_store_config_init(void);

#define TAG "relay"
#define NONE BLE_HS_CONN_HANDLE_NONE

enum {                    // Mac -> relay
    HELLO = 0x01,         // -> EV_HELLO [nonce 16]
    AUTH = 0x02,          // [HMAC 16] -> EV_AUTH [ok]
    SCAN = 0x10,          // [seconds, 0 stops][name prefix...] -> EV_ADV per report whose name has the prefix, EV_SCAN_END
    CONNECT = 0x11,       // [addr type][addr 6] -> EV_CONNECTED, EV_ATTRs (every attribute), EV_ATTRS_END
    DISCONNECT = 0x12,    // -> EV_DISCONNECTED
    READ = 0x13,          // [handle 2] -> EV_READ
    WRITE = 0x14,         // [handle 2][with response][data...] -> EV_WRITE if with response
    PAIR = 0x15,          // -> EV_ENC
};
enum {                    // relay -> Mac
    EV_HELLO = 0x81,
    EV_AUTH = 0x82,
    EV_ADV = 0x90,        // [addr type][addr 6][rssi][advertising data...]
    EV_SCAN_END = 0x91,
    EV_CONNECTED = 0x92,  // [status 2][mtu 2]
    EV_ATTR = 0x93,       // ([handle 2][type length][type: UUID 2 or 16])... as many as fit the Mac's MTU
    EV_ATTRS_END = 0x94,  // [status 2]
    EV_DISCONNECTED = 0x95,  // [reason 2]
    EV_READ = 0x96,       // [handle 2][status 2][value...]
    EV_WRITE = 0x97,      // [handle 2][status 2]
    EV_NOTIFY = 0x98,     // [handle 2][value...]
    EV_ENC = 0x99,        // [status 2][encrypted][bonded]
    EV_ERROR = 0xee,      // [op][status 2]; statuses are NimBLE's: 0x100 + ATT error, 0x200 + HCI error, ...
};

static const ble_uuid128_t svc_uuid = BLE_UUID128_INIT(0x8d, 0xf2, 0xee, 0x57, 0x96, 0x50, 0x0b, 0x85, 0xd5, 0x45,
                                                       0x0b, 0x61, 0x01, 0x00, 0xaa, 0x5a);
static const ble_uuid128_t cmd_uuid = BLE_UUID128_INIT(0x8d, 0xf2, 0xee, 0x57, 0x96, 0x50, 0x0b, 0x85, 0xd5, 0x45,
                                                       0x0b, 0x61, 0x02, 0x00, 0xaa, 0x5a);
static const ble_uuid128_t evt_uuid = BLE_UUID128_INIT(0x8d, 0xf2, 0xee, 0x57, 0x96, 0x50, 0x0b, 0x85, 0xd5, 0x45,
                                                       0x0b, 0x61, 0x03, 0x00, 0xaa, 0x5a);

static uint16_t mac = NONE, dut = NONE, evt_handle;
static bool subscribed, authed;
static uint8_t nonce[16], own_addr_type;
static char scan_prefix[32];
static size_t scan_prefix_len;

static void put16(uint8_t *p, uint16_t v)
{
    p[0] = v;
    p[1] = v >> 8;
}

/* An event to the Mac: h[0] is its code, then fixed fields, then data */
static void emit(const uint8_t *h, size_t nh, const void *data, size_t nd)
{
    if (mac == NONE || !subscribed) {
        return;
    }
    struct os_mbuf *om = ble_hs_mbuf_from_flat(h, nh);
    if (!om || (nd && os_mbuf_append(om, data, nd))) {
        os_mbuf_free_chain(om);
        ESP_LOGW(TAG, "event 0x%02x lost: no buffer", h[0]);
        return;
    }
    int rc = ble_gatts_notify_custom(mac, evt_handle, om);
    if (rc) {
        ESP_LOGW(TAG, "event 0x%02x lost: %d", h[0], rc);
    }
}

static void emit_status(uint8_t ev, uint16_t handle, int status)
{
    uint8_t h[5] = {ev};
    put16(h + 1, handle);
    put16(h + 3, status);
    emit(h, sizeof h, NULL, 0);
}

static void emit_error(uint8_t op, int status)
{
    uint8_t h[4] = {EV_ERROR, op};
    put16(h + 2, status);
    emit(h, sizeof h, NULL, 0);
}

static int scan_event(struct ble_gap_event *e, void *arg)
{
    if (e->type == BLE_GAP_EVENT_DISC) {
        struct ble_hs_adv_fields f;
        if (ble_hs_adv_parse_fields(&f, e->disc.data, e->disc.length_data) || !f.name ||
            f.name_len < scan_prefix_len || memcmp(f.name, scan_prefix, scan_prefix_len)) {
            return 0;
        }
        uint8_t h[9] = {EV_ADV, e->disc.addr.type};
        memcpy(h + 2, e->disc.addr.val, 6);
        h[8] = e->disc.rssi;
        emit(h, sizeof h, e->disc.data, e->disc.length_data);
    } else if (e->type == BLE_GAP_EVENT_DISC_COMPLETE) {
        emit((uint8_t[]){EV_SCAN_END}, 1, NULL, 0);
    }
    return 0;
}

/* Every attribute of the board, as ATT Find Information lists them (descriptor discovery from handle 0): the Mac
 * finds characteristic values and their CCCDs in it. Packed into few events: one each, a recorder's 40-odd ran the
 * relay out of buffers and some were lost (10-06). */
static int attr_found(uint16_t conn, const struct ble_gatt_error *err, uint16_t chr_val_handle,
                      const struct ble_gatt_dsc *dsc, void *arg)
{
    static uint8_t ev[BLE_ATT_ATTR_MAX_LEN];
    static size_t n;
    uint8_t len = dsc && dsc->uuid.u.type == BLE_UUID_TYPE_16 ? 2 : 16;
    if (n && (err->status || n + 3 + len > MIN(ble_att_mtu(mac) - 3, sizeof ev))) {
        emit(ev, n, NULL, 0);
        n = 0;
    }
    if (err->status) {
        emit_status(EV_ATTRS_END, 0, err->status == BLE_HS_EDONE ? 0 : err->status);
        return 0;
    }
    if (!n) {
        ev[n++] = EV_ATTR;
    }
    put16(ev + n, dsc->handle);
    ev[n + 2] = len;
    if (len == 2) {
        put16(ev + n + 3, dsc->uuid.u16.value);
    } else {
        memcpy(ev + n + 3, dsc->uuid.u128.value, 16);
    }
    n += 3 + len;
    return 0;
}

static int mtu_done(uint16_t conn, const struct ble_gatt_error *err, uint16_t mtu, void *arg)
{
    uint8_t h[5] = {EV_CONNECTED};
    put16(h + 3, ble_att_mtu(conn));
    emit(h, sizeof h, NULL, 0);
    int rc = ble_gattc_disc_all_dscs(conn, 0, 0xffff, attr_found, NULL);
    if (rc) {
        emit_status(EV_ATTRS_END, 0, rc);
    }
    return 0;
}

static int read_done(uint16_t conn, const struct ble_gatt_error *err, struct ble_gatt_attr *attr, void *arg)
{
    static uint8_t v[BLE_ATT_ATTR_MAX_LEN];
    uint16_t n = 0;
    if (!err->status) {
        ble_hs_mbuf_to_flat(attr->om, v, sizeof v, &n);
    }
    uint8_t h[5] = {EV_READ};
    put16(h + 1, (uintptr_t) arg);
    put16(h + 3, err->status);
    emit(h, sizeof h, v, n);
    return 0;
}

static int write_done(uint16_t conn, const struct ble_gatt_error *err, struct ble_gatt_attr *attr, void *arg)
{
    emit_status(EV_WRITE, (uintptr_t) arg, err->status);
    return 0;
}

static int dut_event(struct ble_gap_event *e, void *arg)
{
    struct ble_gap_conn_desc d;
    switch (e->type) {
    case BLE_GAP_EVENT_CONNECT:
        if (e->connect.status) {
            emit_status(EV_CONNECTED, e->connect.status, 0);  // [status][mtu 0]
            break;
        }
        dut = e->connect.conn_handle;
        ESP_LOGI(TAG, "board connected");
        if (ble_gattc_exchange_mtu(dut, mtu_done, NULL)) {
            mtu_done(dut, NULL, 0, NULL);
        }
        break;
    case BLE_GAP_EVENT_DISCONNECT:
        ESP_LOGI(TAG, "board disconnected: 0x%x", e->disconnect.reason);
        dut = NONE;
        emit_status(EV_DISCONNECTED, e->disconnect.reason, 0);
        break;
    case BLE_GAP_EVENT_ENC_CHANGE: {
        ble_gap_conn_find(e->enc_change.conn_handle, &d);
        ESP_LOGI(TAG, "board encryption: status %d bonded %d", e->enc_change.status, d.sec_state.bonded);
        uint8_t h[5] = {EV_ENC};
        put16(h + 1, e->enc_change.status);
        h[3] = d.sec_state.encrypted;
        h[4] = d.sec_state.bonded;
        emit(h, sizeof h, NULL, 0);
        break;
    }
    case BLE_GAP_EVENT_NOTIFY_RX: {
        static uint8_t v[BLE_ATT_ATTR_MAX_LEN];
        uint16_t n = 0;
        ble_hs_mbuf_to_flat(e->notify_rx.om, v, sizeof v, &n);
        uint8_t h[3] = {EV_NOTIFY};
        put16(h + 1, e->notify_rx.attr_handle);
        emit(h, sizeof h, v, n);
        break;
    }
    case BLE_GAP_EVENT_REPEAT_PAIRING:  // the board lost our bond: pair afresh
        ble_gap_conn_find(e->repeat_pairing.conn_handle, &d);
        ble_store_util_delete_peer(&d.peer_id_addr);
        return BLE_GAP_REPEAT_PAIRING_RETRY;
    }
    return 0;
}

/* The board's link ends with the Mac's: a recorder takes one connection, and a relay holding it would lock out its
 * phone */
static void drop_dut(void)
{
    ble_gap_disc_cancel();
    ble_gap_conn_cancel();
    if (dut != NONE) {
        ble_gap_terminate(dut, BLE_ERR_REM_USER_CONN_TERM);
    }
}

static void command(const uint8_t *p, uint16_t n)
{
    uint8_t op = p[0];
    int rc = 0;
    if (op == HELLO) {
        uint8_t h[1] = {EV_HELLO};
        emit(h, 1, nonce, sizeof nonce);
        return;
    }
    if (op == AUTH) {
        uint8_t want[32];
        mbedtls_md_hmac(mbedtls_md_info_from_type(MBEDTLS_MD_SHA256), relay_key, sizeof relay_key, nonce,
                        sizeof nonce, want);
        authed = n == 17 && !memcmp(want, p + 1, 16);
        esp_fill_random(nonce, sizeof nonce);  // one try per nonce
        ESP_LOGI(TAG, "auth %s", authed ? "ok" : "FAILED");
        emit((uint8_t[]){EV_AUTH, authed}, 2, NULL, 0);
        return;
    }
    if (!authed) {
        emit_error(op, BLE_HS_EAUTHEN);
        return;
    }
    uint16_t handle = n >= 3 ? p[1] | p[2] << 8 : 0;
    switch (op) {
    case SCAN: {
        ble_gap_disc_cancel();
        if (n < 2 || !p[1]) {
            break;
        }
        scan_prefix_len = MIN(n - 2, sizeof scan_prefix);
        memcpy(scan_prefix, p + 2, scan_prefix_len);
        struct ble_gap_disc_params dp = {.passive = 0, .filter_duplicates = 0};
        rc = ble_gap_disc(own_addr_type, p[1] * 1000, &dp, scan_event, NULL);
        break;
    }
    case CONNECT: {
        if (n != 8) {
            rc = BLE_HS_EINVAL;
            break;
        }
        if (dut != NONE) {
            rc = BLE_HS_EALREADY;
            break;
        }
        ble_gap_disc_cancel();
        ble_addr_t a = {.type = p[1]};
        memcpy(a.val, p + 2, 6);
        // 7.5-15 ms: legacy DFU waits for a notification every 8 packets
        struct ble_gap_conn_params cp = {.scan_itvl = 0x10, .scan_window = 0x10, .itvl_min = 6, .itvl_max = 12,
                                         .supervision_timeout = 500};
        rc = ble_gap_connect(own_addr_type, &a, 10000, &cp, dut_event, NULL);
        break;
    }
    case DISCONNECT:
        drop_dut();
        break;
    case READ:
        rc = dut == NONE ? BLE_HS_ENOTCONN : n != 3 ? BLE_HS_EINVAL
                                                    : ble_gattc_read(dut, handle, read_done, (void *) (uintptr_t) handle);
        break;
    case WRITE:
        if (dut == NONE || n < 4) {
            rc = dut == NONE ? BLE_HS_ENOTCONN : BLE_HS_EINVAL;
        } else if (p[3]) {
            rc = ble_gattc_write_flat(dut, handle, p + 4, n - 4, write_done, (void *) (uintptr_t) handle);
        } else {
            rc = ble_gattc_write_no_rsp_flat(dut, handle, p + 4, n - 4);
        }
        break;
    case PAIR:
        rc = dut == NONE ? BLE_HS_ENOTCONN : ble_gap_security_initiate(dut);
        if (rc == BLE_HS_EALREADY) {
            rc = 0;  // the board asked for it already: EV_ENC follows
        }
        break;
    default:
        rc = BLE_HS_EINVAL;
    }
    if (rc) {
        ESP_LOGW(TAG, "op 0x%02x: %d", op, rc);
        emit_error(op, rc);
    }
}

static int relay_access(uint16_t conn, uint16_t attr, struct ble_gatt_access_ctxt *ctxt, void *arg)
{
    static uint8_t p[BLE_ATT_ATTR_MAX_LEN];
    uint16_t n;
    if (ctxt->op != BLE_GATT_ACCESS_OP_WRITE_CHR || conn != mac) {
        return BLE_ATT_ERR_WRITE_NOT_PERMITTED;
    }
    if (ble_hs_mbuf_to_flat(ctxt->om, p, sizeof p, &n) || !n) {
        return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;
    }
    command(p, n);
    return 0;
}

static const struct ble_gatt_svc_def services[] = {
    {.type = BLE_GATT_SVC_TYPE_PRIMARY,
     .uuid = &svc_uuid.u,
     .characteristics =
         (struct ble_gatt_chr_def[]){
             {.uuid = &cmd_uuid.u, .access_cb = relay_access, .flags = BLE_GATT_CHR_F_WRITE | BLE_GATT_CHR_F_WRITE_NO_RSP},
             {.uuid = &evt_uuid.u, .access_cb = relay_access, .flags = BLE_GATT_CHR_F_NOTIFY, .val_handle = &evt_handle},
             {0}}},
    {0}};

static void advertise(void);

static int mac_event(struct ble_gap_event *e, void *arg)
{
    switch (e->type) {
    case BLE_GAP_EVENT_CONNECT:
        if (e->connect.status) {
            advertise();
            break;
        }
        mac = e->connect.conn_handle;
        authed = subscribed = false;
        esp_fill_random(nonce, sizeof nonce);
        ESP_LOGI(TAG, "Mac connected");
        break;
    case BLE_GAP_EVENT_DISCONNECT:
        ESP_LOGI(TAG, "Mac disconnected: 0x%x", e->disconnect.reason);
        mac = NONE;
        authed = subscribed = false;
        drop_dut();
        advertise();
        break;
    case BLE_GAP_EVENT_SUBSCRIBE:
        if (e->subscribe.attr_handle == evt_handle) {
            subscribed = e->subscribe.cur_notify;
        }
        break;
    case BLE_GAP_EVENT_ADV_COMPLETE:
        if (mac == NONE) {
            advertise();
        }
        break;
    }
    return 0;
}

/* Connectable while no Mac is connected; the relay service UUID in the advertising data, the name in the scan
 * response */
static void advertise(void)
{
    struct ble_hs_adv_fields f = {.flags = BLE_HS_ADV_F_DISC_GEN | BLE_HS_ADV_F_BREDR_UNSUP,
                                  .uuids128 = &svc_uuid,
                                  .num_uuids128 = 1,
                                  .uuids128_is_complete = 1};
    const char *name = ble_svc_gap_device_name();
    struct ble_hs_adv_fields r = {.name = (const uint8_t *) name, .name_len = strlen(name), .name_is_complete = 1};
    struct ble_gap_adv_params ap = {.conn_mode = BLE_GAP_CONN_MODE_UND, .disc_mode = BLE_GAP_DISC_MODE_GEN};
    int rc = ble_gap_adv_set_fields(&f) || ble_gap_adv_rsp_set_fields(&r) ||
             ble_gap_adv_start(own_addr_type, NULL, BLE_HS_FOREVER, &ap, mac_event, NULL);
    if (rc) {
        ESP_LOGE(TAG, "advertising failed");
    }
}

static void on_sync(void)
{
    uint8_t a[6];
    char name[16];
    ble_hs_util_ensure_addr(0);
    ble_hs_id_infer_auto(0, &own_addr_type);
    ble_hs_id_copy_addr(own_addr_type, a, NULL);
    snprintf(name, sizeof name, "relay-%02X%02X", a[1], a[0]);  // not LR-...: the app takes those for recorders
    ble_svc_gap_device_name_set(name);
    ESP_LOGI(TAG, "%s up", name);
    advertise();
}

static void host_task(void *param)
{
    nimble_port_run();
    nimble_port_freertos_deinit();
}

void app_main(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);
    ESP_ERROR_CHECK(nimble_port_init());
    ble_hs_cfg.sync_cb = on_sync;
    ble_hs_cfg.store_status_cb = ble_store_util_status_rr;
    // as a phone: LESC Just Works, bonded (the recorders' encrypted characteristics, their DFU trigger)
    ble_hs_cfg.sm_io_cap = BLE_SM_IO_CAP_NO_IO;
    ble_hs_cfg.sm_bonding = 1;
    ble_hs_cfg.sm_sc = 1;
    ble_hs_cfg.sm_our_key_dist = ble_hs_cfg.sm_their_key_dist = BLE_SM_PAIR_KEY_DIST_ENC | BLE_SM_PAIR_KEY_DIST_ID;
    ble_svc_gap_init();
    ble_svc_gatt_init();
    ESP_ERROR_CHECK(ble_gatts_count_cfg(services));
    ESP_ERROR_CHECK(ble_gatts_add_svcs(services));
    ble_store_config_init();
    nimble_port_freertos_init(host_task);
}
