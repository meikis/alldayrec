#include "transport.h"

#include <string.h>
#include <hal/nrf_power.h>
#include <zephyr/bluetooth/bluetooth.h>
#include <zephyr/bluetooth/conn.h>
#include <zephyr/bluetooth/gatt.h>
#include <zephyr/bluetooth/hci.h>
#include <zephyr/bluetooth/l2cap.h>
#include <zephyr/bluetooth/services/bas.h>
#include <zephyr/bluetooth/uuid.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/drivers/sensor.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/settings/settings.h>
#include <zephyr/sys/atomic.h>
#include <zephyr/sys/byteorder.h>
#include <zephyr/sys/ring_buffer.h>

#include "config.h"
#include "utils.h"
// #include "nfc.h"
#include "button.h"
#include "lib/battery/battery.h"
#include "mic.h"
#include "sdcard.h"
#include "speaker.h"
#include "lr_log.h"
#include "lr_store.h"
// #include "friend.h"
LOG_MODULE_REGISTER(transport, CONFIG_LOG_DEFAULT_LEVEL);

extern bool is_connected;
struct bt_conn *current_connection = NULL;
uint16_t current_mtu = 0;
uint16_t current_package_index = 0;

//
// Internal
//

struct k_mutex write_sdcard_mutex;
K_SEM_DEFINE(pusher_wake_sem, 0, 1);
static atomic_t connection_generation;

static ssize_t audio_data_write_handler(struct bt_conn *conn,
                                        const struct bt_gatt_attr *attr,
                                        const void *buf,
                                        uint16_t len,
                                        uint16_t offset,
                                        uint8_t flags);

static struct bt_conn_cb _callback_references;
static void audio_ccc_config_changed_handler(const struct bt_gatt_attr *attr, uint16_t value);
static ssize_t audio_data_read_characteristic(struct bt_conn *conn,
                                              const struct bt_gatt_attr *attr,
                                              void *buf,
                                              uint16_t len,
                                              uint16_t offset);
static ssize_t audio_codec_read_characteristic(struct bt_conn *conn,
                                               const struct bt_gatt_attr *attr,
                                               void *buf,
                                               uint16_t len,
                                               uint16_t offset);

static void dfu_ccc_config_changed_handler(const struct bt_gatt_attr *attr, uint16_t value);
static ssize_t dfu_control_point_write_handler(struct bt_conn *conn,
                                               const struct bt_gatt_attr *attr,
                                               const void *buf,
                                               uint16_t len,
                                               uint16_t offset,
                                               uint8_t flags);

//
// Service and Characteristic
//
// Audio service with UUID 19B10000-E8F2-537E-4F6C-D104768A1214
// exposes following characteristics:
// - Audio data (UUID 19B10001-E8F2-537E-4F6C-D104768A1214) to send audio data (read/notify)
// - Audio codec (UUID 19B10002-E8F2-537E-4F6C-D104768A1214) to send audio codec type (read)
// TODO: The current audio service UUID seems to come from old Intel sample code,
// we should change it to UUID 814b9b7c-25fd-4acd-8604-d28877beee6d
static struct bt_uuid_128 audio_service_uuid =
    BT_UUID_INIT_128(BT_UUID_128_ENCODE(0x19B10000, 0xE8F2, 0x537E, 0x4F6C, 0xD104768A1214));
static struct bt_uuid_128 audio_characteristic_data_uuid =
    BT_UUID_INIT_128(BT_UUID_128_ENCODE(0x19B10001, 0xE8F2, 0x537E, 0x4F6C, 0xD104768A1214));
static struct bt_uuid_128 audio_characteristic_format_uuid =
    BT_UUID_INIT_128(BT_UUID_128_ENCODE(0x19B10002, 0xE8F2, 0x537E, 0x4F6C, 0xD104768A1214));
static struct bt_uuid_128 audio_characteristic_speaker_uuid =
    BT_UUID_INIT_128(BT_UUID_128_ENCODE(0x19B10003, 0xE8F2, 0x537E, 0x4F6C, 0xD104768A1214));

static struct bt_gatt_attr audio_service_attr[] = {
    BT_GATT_PRIMARY_SERVICE(&audio_service_uuid),
    BT_GATT_CHARACTERISTIC(&audio_characteristic_data_uuid.uuid,
                           BT_GATT_CHRC_READ | BT_GATT_CHRC_NOTIFY,
                           BT_GATT_PERM_READ_ENCRYPT,
                           audio_data_read_characteristic,
                           NULL,
                           NULL),
    BT_GATT_CCC(audio_ccc_config_changed_handler, BT_GATT_PERM_READ | BT_GATT_PERM_WRITE_ENCRYPT),
    BT_GATT_CHARACTERISTIC(&audio_characteristic_format_uuid.uuid,
                           BT_GATT_CHRC_READ,
                           BT_GATT_PERM_READ_ENCRYPT,  // the clients' first read: an unpaired phone pairs here
                           audio_codec_read_characteristic,
                           NULL,
                           NULL),
#ifdef CONFIG_OMI_ENABLE_SPEAKER
    BT_GATT_CHARACTERISTIC(&audio_characteristic_speaker_uuid.uuid,
                           BT_GATT_CHRC_WRITE | BT_GATT_CHRC_NOTIFY,
                           BT_GATT_PERM_WRITE,
                           NULL,
                           audio_data_write_handler,
                           NULL),
    BT_GATT_CCC(audio_ccc_config_changed_handler, BT_GATT_PERM_READ | BT_GATT_PERM_WRITE), //
#endif

};

static struct bt_gatt_service audio_service = BT_GATT_SERVICE(audio_service_attr);

// Nordic Legacy DFU service with UUID 00001530-1212-EFDE-1523-785FEABCD123
// exposes following characteristics:
// - Control point (UUID 00001531-1212-EFDE-1523-785FEABCD123) to start the OTA update process (write/notify)
static struct bt_uuid_128 dfu_service_uuid =
    BT_UUID_INIT_128(BT_UUID_128_ENCODE(0x00001530, 0x1212, 0xEFDE, 0x1523, 0x785FEABCD123));
static struct bt_uuid_128 dfu_control_point_uuid =
    BT_UUID_INIT_128(BT_UUID_128_ENCODE(0x00001531, 0x1212, 0xEFDE, 0x1523, 0x785FEABCD123));
// packet and revision as the Adafruit app-side DFU service has them: Nordic's Android DFU library looks for both, and
// revision 1 tells it this is the application, to be sent into the bootloader first
static struct bt_uuid_128 dfu_packet_uuid =
    BT_UUID_INIT_128(BT_UUID_128_ENCODE(0x00001532, 0x1212, 0xEFDE, 0x1523, 0x785FEABCD123));
static struct bt_uuid_128 dfu_revision_uuid =
    BT_UUID_INIT_128(BT_UUID_128_ENCODE(0x00001534, 0x1212, 0xEFDE, 0x1523, 0x785FEABCD123));
static ssize_t dfu_ignore(struct bt_conn *conn, const struct bt_gatt_attr *attr, const void *buf, uint16_t len,
                          uint16_t offset, uint8_t flags)
{
    return len;
}
static ssize_t dfu_revision(struct bt_conn *conn, const struct bt_gatt_attr *attr, void *buf, uint16_t len, uint16_t offset)
{
    uint8_t v[2] = {1, 0};
    return bt_gatt_attr_read(conn, attr, buf, len, offset, v, sizeof(v));
}

static struct bt_gatt_attr dfu_service_attr[] = {
    BT_GATT_PRIMARY_SERVICE(&dfu_service_uuid),
    BT_GATT_CHARACTERISTIC(&dfu_control_point_uuid.uuid,
                           BT_GATT_CHRC_WRITE | BT_GATT_CHRC_NOTIFY,
                           BT_GATT_PERM_WRITE_ENCRYPT,  // only a bonded phone sends the recorder into its bootloader
                           NULL,
                           dfu_control_point_write_handler,
                           NULL),
    BT_GATT_CCC(dfu_ccc_config_changed_handler, BT_GATT_PERM_READ | BT_GATT_PERM_WRITE_ENCRYPT),
    BT_GATT_CHARACTERISTIC(&dfu_packet_uuid.uuid, BT_GATT_CHRC_WRITE_WITHOUT_RESP, BT_GATT_PERM_WRITE_ENCRYPT, NULL,
                           dfu_ignore, NULL),
    BT_GATT_CHARACTERISTIC(&dfu_revision_uuid.uuid, BT_GATT_CHRC_READ, BT_GATT_PERM_READ, dfu_revision, NULL, NULL),
};

static struct bt_gatt_service dfu_service = BT_GATT_SERVICE(dfu_service_attr);
// Acceleration data
// this code activates the onboard accelerometer. some cute ideas may include shaking the necklace to color strobe
//
static struct sensors mega_sensor;
static struct device *lsm6dsl_dev;
// Arbritrary uuid, feel free to change
static struct bt_uuid_128 accel_uuid =
    BT_UUID_INIT_128(BT_UUID_128_ENCODE(0x32403790, 0x0000, 0x1000, 0x7450, 0xBF445E5829A2));
static struct bt_uuid_128 accel_uuid_x =
    BT_UUID_INIT_128(BT_UUID_128_ENCODE(0x32403791, 0x0000, 0x1000, 0x7450, 0xBF445E5829A2));

static void accel_ccc_config_changed_handler(const struct bt_gatt_attr *attr, uint16_t value);
static ssize_t accel_data_read_characteristic(struct bt_conn *conn,
                                              const struct bt_gatt_attr *attr,
                                              void *buf,
                                              uint16_t len,
                                              uint16_t offset);

static struct bt_gatt_attr accel_service_attr[] = {
    BT_GATT_PRIMARY_SERVICE(&accel_uuid), // primary description
    BT_GATT_CHARACTERISTIC(&accel_uuid_x.uuid,
                           BT_GATT_CHRC_READ | BT_GATT_CHRC_NOTIFY,
                           BT_GATT_PERM_READ,
                           accel_data_read_characteristic,
                           NULL,
                           NULL),                                                          // data type
    BT_GATT_CCC(accel_ccc_config_changed_handler, BT_GATT_PERM_READ | BT_GATT_PERM_WRITE), // scheduler
};
static struct bt_gatt_service accel_service = BT_GATT_SERVICE(accel_service_attr);

static ssize_t accel_data_read_characteristic(struct bt_conn *conn,
                                              const struct bt_gatt_attr *attr,
                                              void *buf,
                                              uint16_t len,
                                              uint16_t offset)
{
    LOG_INF("Acceleration data read characteristic");
    int axis_mode = 6; // 3 for accel, 6 for (also) gyro
    return bt_gatt_attr_read(conn, attr, buf, len, offset, &axis_mode, sizeof(axis_mode));
}

#define ACCEL_REFRESH_INTERVAL 1000 // 1.0 seconds

void broadcast_accel(struct k_work *work_item);
K_WORK_DELAYABLE_DEFINE(accel_work, broadcast_accel);

void broadcast_accel(struct k_work *work_item)
{

    sensor_sample_fetch_chan(lsm6dsl_dev, SENSOR_CHAN_ACCEL_XYZ);
    sensor_channel_get(lsm6dsl_dev, SENSOR_CHAN_ACCEL_X, &mega_sensor.a_x);
    sensor_channel_get(lsm6dsl_dev, SENSOR_CHAN_ACCEL_Y, &mega_sensor.a_y);
    sensor_channel_get(lsm6dsl_dev, SENSOR_CHAN_ACCEL_Z, &mega_sensor.a_z);

    sensor_sample_fetch_chan(lsm6dsl_dev, SENSOR_CHAN_GYRO_XYZ);
    sensor_channel_get(lsm6dsl_dev, SENSOR_CHAN_GYRO_X, &mega_sensor.g_x);
    sensor_channel_get(lsm6dsl_dev, SENSOR_CHAN_GYRO_Y, &mega_sensor.g_y);
    sensor_channel_get(lsm6dsl_dev, SENSOR_CHAN_GYRO_Z, &mega_sensor.g_z);

    // only time mega sensor is changed is through here (hopefully),  so no chance of race condition
    int err = bt_gatt_notify(current_connection, &accel_service.attrs[1], &mega_sensor, sizeof(mega_sensor));
    if (err) {
        LOG_ERR("Error updating Accelerometer data");
    }
    k_work_reschedule(&accel_work, K_MSEC(ACCEL_REFRESH_INTERVAL));
}

struct gpio_dt_spec accel_gpio_pin = {.port = DEVICE_DT_GET(DT_NODELABEL(gpio1)),
                                      .pin = 8,
                                      .dt_flags = GPIO_INT_DISABLE};

// use d4,d5
static void accel_ccc_config_changed_handler(const struct bt_gatt_attr *attr, uint16_t value)
{
    if (value == BT_GATT_CCC_NOTIFY) {
        LOG_INF("Client subscribed for notifications");
    } else if (value == 0) {
        LOG_INF("Client unsubscribed from notifications");
    } else {
        LOG_ERR("Invalid CCC value: %u", value);
    }
}

int accel_start()
{
    struct sensor_value odr_attr;
    lsm6dsl_dev = DEVICE_DT_GET_ONE(st_lsm6dsl);
    k_msleep(50);
    if (lsm6dsl_dev == NULL) {
        LOG_ERR("Could not get LSM6DSL device");
        return 0;
    }
    if (!device_is_ready(lsm6dsl_dev)) {
        LOG_ERR("LSM6DSL: not ready");
        return 0;
    }
    odr_attr.val1 = 10;
    odr_attr.val2 = 0;

    if (gpio_is_ready_dt(&accel_gpio_pin)) {
        LOG_PRINTK("Speaker Pin ready\n");
    } else {
        LOG_PRINTK("Error setting up speaker Pin\n");
        return -1;
    }
    if (gpio_pin_configure_dt(&accel_gpio_pin, GPIO_OUTPUT_INACTIVE) < 0) {
        LOG_PRINTK("Error setting up Haptic Pin\n");
        return -1;
    }
    gpio_pin_set_dt(&accel_gpio_pin, 1);
    if (sensor_attr_set(lsm6dsl_dev, SENSOR_CHAN_ACCEL_XYZ, SENSOR_ATTR_SAMPLING_FREQUENCY, &odr_attr) < 0) {
        LOG_ERR("Cannot set sampling frequency for Accelerometer.");
        return 0;
    }
    if (sensor_attr_set(lsm6dsl_dev, SENSOR_CHAN_GYRO_XYZ, SENSOR_ATTR_SAMPLING_FREQUENCY, &odr_attr) < 0) {
        LOG_ERR("Cannot set sampling frequency for gyro.");
        return 0;
    }
    if (sensor_sample_fetch(lsm6dsl_dev) < 0) {
        LOG_ERR("Sensor sample update error");
        return 0;
    }

    LOG_INF("Accelerometer is ready for use \n");

    return 1;
}
// Advertisement data: 31 bytes at most, so the board name (LR-X1) here and the full name (LR-X1-3F2A) in the scan response
static const struct bt_data bt_ad[] = {
    BT_DATA_BYTES(BT_DATA_FLAGS, (BT_LE_AD_GENERAL | BT_LE_AD_NO_BREDR)),
    BT_DATA(BT_DATA_UUID128_ALL, audio_service_uuid.val, sizeof(audio_service_uuid.val)),
    BT_DATA(BT_DATA_NAME_SHORTENED, CONFIG_LR_NAME_PREFIX, sizeof(CONFIG_LR_NAME_PREFIX) - 1),
};

static char lr_name[CONFIG_BT_DEVICE_NAME_MAX + 1];

// Scan response data (the DIS UUID left out for room: the service itself is still there)
static struct bt_data bt_sd[] = {
    {.type = BT_DATA_NAME_COMPLETE, .data_len = 0, .data = (const uint8_t *) lr_name},
    BT_DATA(BT_DATA_UUID128_ALL, dfu_service_uuid.val, sizeof(dfu_service_uuid.val)),
};

/* The unit's name: the board name and the last two bytes of its identity address, which stays with the chip */
static void lr_set_name(void)
{
    bt_addr_le_t addr;
    size_t n = 1;
    bt_id_get(&addr, &n);
    snprintk(lr_name, sizeof(lr_name), "%s-%02X%02X", CONFIG_LR_NAME_PREFIX, addr.a.val[1], addr.a.val[0]);
    bt_set_name(lr_name);
    bt_sd[0].data_len = strlen(lr_name);
    LOG_INF("BLE name %s", lr_name);
}

//
// State and Characteristics
//

static void audio_ccc_config_changed_handler(const struct bt_gatt_attr *attr, uint16_t value)
{
    if (value == BT_GATT_CCC_NOTIFY) {
        LOG_INF("Client subscribed for notifications");
    } else if (value == 0) {
        LOG_INF("Client unsubscribed from notifications");
    } else {
        LOG_INF("Invalid CCC value: %u", value);
    }
    k_sem_give(&pusher_wake_sem);
}

static ssize_t audio_data_read_characteristic(struct bt_conn *conn,
                                              const struct bt_gatt_attr *attr,
                                              void *buf,
                                              uint16_t len,
                                              uint16_t offset)
{
    LOG_DBG("audio_data_read_characteristic");
    return bt_gatt_attr_read(conn, attr, buf, len, offset, NULL, 0);
}

static ssize_t audio_codec_read_characteristic(struct bt_conn *conn,
                                               const struct bt_gatt_attr *attr,
                                               void *buf,
                                               uint16_t len,
                                               uint16_t offset)
{
    uint8_t value[1] = {CODEC_ID};
    LOG_DBG("audio_codec_read_characteristic %d", CODEC_ID);
    return bt_gatt_attr_read(conn, attr, buf, len, offset, value, sizeof(value));
}

static ssize_t audio_data_write_handler(struct bt_conn *conn,
                                        const struct bt_gatt_attr *attr,
                                        const void *buf,
                                        uint16_t len,
                                        uint16_t offset,
                                        uint8_t flags)
{
    uint16_t amount = 400;
    int16_t *int16_buf = (int16_t *) buf;
    uint8_t *data = (uint8_t *) buf;
    bt_gatt_notify(conn, attr, &amount, sizeof(amount));
    amount = speak(len, buf);
    return len;
}

//
// DFU Service Handlers
//

static void dfu_ccc_config_changed_handler(const struct bt_gatt_attr *attr, uint16_t value)
{
    if (value == BT_GATT_CCC_NOTIFY) {
        LOG_INF("Client subscribed for notifications");
    } else if (value == 0) {
        LOG_INF("Client unsubscribed from notifications");
    } else {
        LOG_INF("Invalid CCC value: %u", value);
    }
}

static ssize_t dfu_control_point_write_handler(struct bt_conn *conn,
                                               const struct bt_gatt_attr *attr,
                                               const void *buf,
                                               uint16_t len,
                                               uint16_t offset,
                                               uint8_t flags)
{
    LOG_INF("dfu_control_point_write_handler");
    if (len == 1 && ((uint8_t *) buf)[0] == 0x57) {  // ours: into the UF2 bootloader (USB drive), as the 1200 bps touch
        lr_store_shutdown();
        lr_log_reset(0x57);
    } else if (len == 1 && ((uint8_t *) buf)[0] == 0x06) {
        lr_log_reset(0xA8);
    } else if (len == 2 && ((uint8_t *) buf)[0] == 0x01) {
        uint8_t notification_value = 0x10;
        bt_gatt_notify(conn, attr, &notification_value, sizeof(notification_value));
        lr_log_reset(0xA8);
    }
    return len;
}

//
// Battery Service Handlers
//

#define BATTERY_REFRESH_INTERVAL 15000 // 15 seconds

void broadcast_battery_level(struct k_work *work_item);

K_WORK_DELAYABLE_DEFINE(battery_work, broadcast_battery_level);

void broadcast_battery_level(struct k_work *work_item)
{
    uint16_t battery_millivolt;
    uint8_t battery_percentage;
    if (battery_get_millivolt(&battery_millivolt) == 0 &&
        battery_get_percentage(&battery_percentage, battery_millivolt) == 0) {

        LOG_PRINTK("Battery at %d mV (capacity %d%%)\n", battery_millivolt, battery_percentage);

        // Use the Zephyr BAS function to set (and notify) the battery level
        int err = bt_bas_set_battery_level(battery_percentage);
        if (err) {
            LOG_ERR("Error updating battery level: %d", err);
        }
    } else {
        LOG_ERR("Failed to read battery level");
    }

    k_work_reschedule(&battery_work, K_MSEC(BATTERY_REFRESH_INTERVAL));
}

//
// Connection Callbacks
//

static void _transport_connected(struct bt_conn *conn, uint8_t err)
{
    struct bt_conn_info info = {0};

    err = bt_conn_get_info(conn, &info);
    if (err) {
        LOG_ERR("Failed to get connection info (err %d)", err);
        bt_conn_unref(conn);
        return;
    }

    LOG_INF("bluetooth activated");

    if (current_connection != NULL) {
        bt_conn_unref(current_connection);
    }
    current_connection = bt_conn_ref(conn);
    current_mtu = info.le.data_len->tx_max_len;
    LOG_INF("Transport connected");
    lr_log(LR_CONNECT, 0, info.le.interval | info.le.latency << 16, info.le.timeout);
    LOG_DBG("Interval: %d, latency: %d, timeout: %d", info.le.interval, info.le.latency, info.le.timeout);
    LOG_DBG("TX PHY %s, RX PHY %s", phy2str(info.le.phy->tx_phy), phy2str(info.le.phy->rx_phy));
    LOG_DBG("LE data len updated: TX (len: %d time: %d) RX (len: %d time: %d)",
            info.le.data_len->tx_max_len,
            info.le.data_len->tx_max_time,
            info.le.data_len->rx_max_len,
            info.le.data_len->rx_max_time);

    k_work_schedule(&battery_work, K_MSEC(100)); // run immediately
    // ask for encryption at once: a bonded phone starts it with its keys, a new one pairs (if pairing is open). macOS
    // does not pair on its own when a read fails for want of encryption (10-03)
    bt_conn_set_security(conn, BT_SECURITY_L2);

    is_connected = true;
    lr_store_rotate();  // what was recorded offline becomes closed files the phone can take
    atomic_inc(&connection_generation);
    k_sem_give(&pusher_wake_sem);
}

static void _transport_disconnected(struct bt_conn *conn, uint8_t err)
{
    is_connected = false;

    LOG_INF("Transport disconnected");
    lr_log(LR_DISCONNECT, err, 0, 0);

    if (current_connection != NULL) {
        bt_conn_unref(current_connection);
        current_connection = NULL;
    }
    current_mtu = 0;
    k_sem_give(&pusher_wake_sem);
}

static bool _le_param_req(struct bt_conn *conn, struct bt_le_conn_param *param)
{
    LOG_INF("Transport connection parameters update request received.");
    LOG_DBG("Minimum interval: %d, Maximum interval: %d", param->interval_min, param->interval_max);
    LOG_DBG("Latency: %d, Timeout: %d", param->latency, param->timeout);

    return true;
}

static void _le_param_updated(struct bt_conn *conn, uint16_t interval, uint16_t latency, uint16_t timeout)
{
    LOG_INF("Connection parameters updated.");
    lr_log(LR_PARAM, 0, interval | latency << 16, timeout);
    LOG_DBG("[ interval: %d, latency: %d, timeout: %d ]", interval, latency, timeout);
}

static void _le_phy_updated(struct bt_conn *conn, struct bt_conn_le_phy_info *param)
{
    lr_log(LR_PHY, 0, param->tx_phy, param->rx_phy);
}

static void _le_data_length_updated(struct bt_conn *conn, struct bt_conn_le_data_len_info *info)
{
    LOG_DBG("LE data len updated: TX (len: %d time: %d)"
            " RX (len: %d time: %d)",
            info->tx_max_len,
            info->tx_max_time,
            info->rx_max_len,
            info->rx_max_time);
    current_mtu = info->tx_max_len;
    lr_log(LR_MTU, 1, info->tx_max_len, info->rx_max_len);
    k_sem_give(&pusher_wake_sem);
}

static void att_mtu_updated(struct bt_conn *conn, uint16_t tx, uint16_t rx)
{
    lr_log(LR_MTU, 0, tx, rx);
}
static struct bt_gatt_cb gatt_cb = {.att_mtu_updated = att_mtu_updated};

//
// Pairing: a stranger nearby must not listen in or take the recordings. Every characteristic but battery and device
// info needs an encrypted link; a phone pairs (LESC, Just Works) only while the recorder is on USB power, in its first
// CONFIG_LR_PAIRING_WINDOW_S after a reset, or while it has no bond yet: having the recorder in hand is the
// permission. Bonded phones reconnect with their keys, no pairing.
//

static void count_bond(const struct bt_bond_info *info, void *n)
{
    (*(int *) n)++;
}

bool lr_pairing_open(void)
{
    int bonds = 0;
    bt_foreach_bond(BT_ID_DEFAULT, count_bond, &bonds);
    return !bonds || nrf_power_usbregstatus_vbusdet_get(NRF_POWER) ||
           k_uptime_get() < CONFIG_LR_PAIRING_WINDOW_S * 1000LL;
}

static enum bt_security_err pairing_accept(struct bt_conn *conn, const struct bt_conn_pairing_feat *const feat)
{
    bool open = lr_pairing_open();
    LOG_INF("pairing request: %s", open ? "accepted" : "refused (plug in USB or reset the recorder)");
    return open ? BT_SECURITY_ERR_SUCCESS : BT_SECURITY_ERR_PAIR_NOT_ALLOWED;
}

static void pairing_complete(struct bt_conn *conn, bool bonded)
{
    LOG_INF("paired, bonded %d", bonded);
    lr_log(LR_PAIRED, bonded, 0, 0);
}

static void pairing_failed(struct bt_conn *conn, enum bt_security_err reason)
{
    LOG_WRN("pairing failed: %d", reason);
    lr_log(LR_PAIR_FAIL, reason, 0, 0);
}

/* A phone that lost its keys (app reinstalled, Bluetooth reset) cannot encrypt with ours: while pairing is open the
 * old bond goes, so it pairs afresh on its next connection (10-03: the Mac stuck at "PIN or key missing") */
static void security_changed(struct bt_conn *conn, bt_security_t level, enum bt_security_err err)
{
    LOG_INF("security level %d, err %d", level, err);
    lr_log(LR_SECURITY, level, err, 0);
    if (err == BT_SECURITY_ERR_PIN_OR_KEY_MISSING && lr_pairing_open()) {
        LOG_WRN("stale bond dropped");
        lr_log(LR_FORGET, 1, 1, 0);
        bt_unpair(BT_ID_DEFAULT, bt_conn_get_dst(conn));
    }
}

static struct bt_conn_auth_cb auth_cb = {.pairing_accept = pairing_accept};
static struct bt_conn_auth_info_cb auth_info_cb = {.pairing_complete = pairing_complete, .pairing_failed = pairing_failed};

static struct bt_conn_cb _callback_references = {
    .connected = _transport_connected,
    .disconnected = _transport_disconnected,
    .le_param_req = _le_param_req,
    .le_param_updated = _le_param_updated,
    .le_phy_updated = _le_phy_updated,
    .le_data_len_updated = _le_data_length_updated,
    .security_changed = security_changed,
};

//
// Ring Buffer
//

#define NET_BUFFER_HEADER_SIZE 3
#define RING_BUFFER_HEADER_SIZE 6  // size:u16, frame number:u32
uint32_t transport_frame_no;  // frames the codec has made since boot, the offline store's clock
static uint32_t tx_frame_no;
// Frames queued by their real size (header + Opus, ~46 B), not in fixed 326 B slots: the same 16 KB hold ~3.5 s, so a
// card that stalls for most of a second (769 ms seen, 10-04) loses nothing; fixed slots held 320 ms
static uint8_t tx_queue[16384];
static uint8_t tx_buffer[CODEC_OUTPUT_MAX_BYTES + RING_BUFFER_HEADER_SIZE];
static uint8_t tx_buffer_2[CODEC_OUTPUT_MAX_BYTES + RING_BUFFER_HEADER_SIZE];
static uint32_t tx_buffer_size = 0;
static struct ring_buf ring_buf;
static bool tx_frame_pending = false;
static uint32_t tx_gatt_offset = 0;
static uint8_t tx_gatt_index = 0;

static bool write_to_tx_queue(uint8_t *data, size_t size)
{
    if (size > CODEC_OUTPUT_MAX_BYTES) {
        return false;
    }

    // Copy data (TODO: Avoid this copy)
    tx_buffer_2[0] = size & 0xFF;
    tx_buffer_2[1] = (size >> 8) & 0xFF;
    sys_put_le32(transport_frame_no++, tx_buffer_2 + 2);  // counted even if the queue is full: a gap, not a shift
    memcpy(tx_buffer_2 + RING_BUFFER_HEADER_SIZE, data, size);

    // Write to ring buffer: whole or not at all
    if (ring_buf_space_get(&ring_buf) < RING_BUFFER_HEADER_SIZE + size) {
        return false;
    }
    ring_buf_put(&ring_buf, tx_buffer_2, RING_BUFFER_HEADER_SIZE + size);
    k_sem_give(&pusher_wake_sem);
    return true;
}

static bool load_tx_frame(void)
{
    if (tx_frame_pending) {
        return true;
    }

    if (ring_buf_size_get(&ring_buf) < RING_BUFFER_HEADER_SIZE) {
        return false;
    }
    ring_buf_get(&ring_buf, tx_buffer, RING_BUFFER_HEADER_SIZE);  // a frame is put whole: its data follows
    ring_buf_get(&ring_buf, tx_buffer + RING_BUFFER_HEADER_SIZE, tx_buffer[0] + (tx_buffer[1] << 8));

    tx_buffer_size = tx_buffer[0] + (tx_buffer[1] << 8);
    tx_frame_no = sys_get_le32(tx_buffer + 2);
    tx_frame_pending = true;
    tx_gatt_offset = 0;
    tx_gatt_index = 0;
    return true;
}

static void consume_tx_frame(void)
{
    tx_frame_pending = false;
    tx_buffer_size = 0;
    tx_gatt_offset = 0;
    tx_gatt_index = 0;
}

//
// Pusher
//

// Thread
K_THREAD_STACK_DEFINE(pusher_stack, 4096);
struct k_thread pusher_thread;  // lr_log.c watches its stack
static uint16_t packet_next_index = 0;
static uint8_t pusher_temp_data[CODEC_OUTPUT_MAX_BYTES + NET_BUFFER_HEADER_SIZE];

static bool push_to_gatt(struct bt_conn *conn)
{
    if (!load_tx_frame()) {
        return false;
    }

    uint8_t *buffer = tx_buffer + RING_BUFFER_HEADER_SIZE;
    const int max_retries = 3;

    while (tx_gatt_offset < tx_buffer_size) {
        uint32_t id = packet_next_index++;
        uint32_t packet_size = MIN(current_mtu - NET_BUFFER_HEADER_SIZE, tx_buffer_size - tx_gatt_offset);
        pusher_temp_data[0] = id & 0xFF;
        pusher_temp_data[1] = (id >> 8) & 0xFF;
        pusher_temp_data[2] = tx_gatt_index;
        memcpy(pusher_temp_data + NET_BUFFER_HEADER_SIZE, buffer + tx_gatt_offset, packet_size);

        int retry_count = 0;
        while (retry_count < max_retries) {
            int err =
                bt_gatt_notify(conn, &audio_service.attrs[1], pusher_temp_data, packet_size + NET_BUFFER_HEADER_SIZE);

            if (err) {
                LOG_DBG("bt_gatt_notify failed (err %d)", err);
                LOG_DBG("MTU: %d, packet_size: %d", current_mtu, packet_size + NET_BUFFER_HEADER_SIZE);
                k_sleep(K_MSEC(1));
                retry_count++;
                continue;
            }

            break;
        }

        if (retry_count >= max_retries) {
            LOG_ERR("Failed to send packet after %d retries", max_retries);
            return false;
        }

        tx_gatt_offset += packet_size;
        tx_gatt_index++;
    }

    consume_tx_frame();
    return true;
}
// While no phone takes the audio, frames go to the SD card (lr_store.c)
static bool write_to_storage(void)
{
    if (!load_tx_frame()) {
        return false;
    }
    k_mutex_lock(&write_sdcard_mutex, K_FOREVER);
    if (is_sd_on()) {
        lr_store_frame(tx_frame_no, tx_buffer + RING_BUFFER_HEADER_SIZE, tx_buffer_size);
    }
    k_mutex_unlock(&write_sdcard_mutex);
    consume_tx_frame();
    return true;
}

void pusher(void)
{
    k_msleep(500);

    while (1) {
        k_sem_take(&pusher_wake_sem, K_FOREVER);

        while (tx_frame_pending || !ring_buf_is_empty(&ring_buf)) {
            struct bt_conn *conn = current_connection;
            if (conn) {
                conn = bt_conn_ref(conn);
            }

            bool valid = true;
            if (current_mtu < MINIMAL_PACKET_SIZE) {
                valid = false;
            } else if (!conn) {
                valid = false;
            } else {
                valid = bt_gatt_is_subscribed(conn, &audio_service.attrs[1], BT_GATT_CCC_NOTIFY);
            }

            bool progressed = false;
            if (!valid) {  // connected but not listening (yet) counts as offline: the audio is kept
                progressed = write_to_storage();
            }

            if (valid) {
                progressed = push_to_gatt(conn);
            }

            if (conn) {
                bt_conn_unref(conn);
            }

            if (!progressed) {
                break;
            }

            k_yield();
        }
    }
}
//
// Public functions
//
int bt_off()
{
    // First disconnect any active connections
    if (current_connection != NULL) {
        bt_conn_disconnect(current_connection, BT_HCI_ERR_REMOTE_USER_TERM_CONN);
        bt_conn_unref(current_connection);
        current_connection = NULL;
    }

    // Stop advertising
    int err = bt_le_adv_stop();
    if (err) {
        LOG_ERR("Failed to stop Bluetooth advertising %d", err);
    }

    // Disable Bluetooth
    err = bt_disable();
    if (err) {
        LOG_ERR("Failed to disable Bluetooth %d", err);
    }

    // Turn off other peripherals
    k_mutex_lock(&write_sdcard_mutex, K_FOREVER);
    sd_off();
    k_mutex_unlock(&write_sdcard_mutex);
    mic_off();

    // Ensure all Bluetooth resources are cleaned up
    is_connected = false;
    current_mtu = 0;
    k_sem_give(&pusher_wake_sem);

    return 0;
}
int bt_on()
{
    int err = bt_enable(NULL);
    bt_le_adv_start(BT_LE_ADV_CONN, bt_ad, ARRAY_SIZE(bt_ad), bt_sd, ARRAY_SIZE(bt_sd));
    sd_on();
    mic_on();
    k_sem_give(&pusher_wake_sem);

    return 0;
}

// periodic advertising
int transport_start()
{
    k_mutex_init(&write_sdcard_mutex);

    // Configure callbacks
    bt_conn_cb_register(&_callback_references);
    bt_gatt_cb_register(&gatt_cb);

    // Enable Bluetooth
    int err = bt_enable(NULL);
    if (err) {
        LOG_ERR("Transport bluetooth init failed (err %d)", err);
        return err;
    }
    LOG_INF("Transport bluetooth initialized");
    settings_load();  // the bonds and the identity
    bt_conn_auth_cb_register(&auth_cb);
    bt_conn_auth_info_cb_register(&auth_info_cb);
    lr_set_name();

    //  Enable button
#ifdef CONFIG_OMI_ENABLE_BUTTON
    register_button_service();
#endif

#ifdef CONFIG_OMI_ENABLE_SPEAKER
    register_speaker_service();
#endif

    // Start advertising
    bt_gatt_service_register(&audio_service);
    bt_gatt_service_register(&dfu_service);
    err = bt_le_adv_start(BT_LE_ADV_CONN, bt_ad, ARRAY_SIZE(bt_ad), bt_sd, ARRAY_SIZE(bt_sd));
    if (err) {
        LOG_ERR("Transport advertising failed to start (err %d)", err);
        return err;
    } else {
        LOG_INF("Advertising successfully started");
    }

    int battErr = 0;
    battErr |= battery_init();
    battErr |= battery_charge_start();
    if (battErr) {
        LOG_ERR("Battery init failed (err %d)", battErr);
    } else {
        LOG_INF("Battery initialized");
    }

    // Start pusher
    ring_buf_init(&ring_buf, sizeof(tx_queue), tx_queue);
    k_thread_create(&pusher_thread,
                    pusher_stack,
                    K_THREAD_STACK_SIZEOF(pusher_stack),
                    (k_thread_entry_t) pusher,
                    NULL,
                    NULL,
                    NULL,
                    K_PRIO_PREEMPT(7),
                    0,
                    K_NO_WAIT);

    return 0;
}

struct bt_conn *get_current_connection()
{
    return current_connection;
}

int broadcast_audio_packets(uint8_t *buffer, size_t size)
{
    int retry_count = 0;
    const int max_retries = 3;

    while (retry_count < max_retries && !write_to_tx_queue(buffer, size)) {
        k_sleep(K_MSEC(1));
        retry_count++;
    }

    if (retry_count >= max_retries) {
        LOG_ERR("Failed to write to tx queue after %d retries", max_retries);
        return -1;
    }

    return 0;
}

void accel_off()
{
    gpio_pin_set_dt(&accel_gpio_pin, 0);
}