#include <hal/nrf_power.h>
#include <zephyr/drivers/disk.h>
#include <zephyr/logging/log.h>
#include <zephyr/storage/disk_access.h>
#include <zephyr/usb/class/usbd_msc.h>
#include <zephyr/drivers/uart.h>
#include <zephyr/sys/iterable_sections.h>
#include <zephyr/usb/usbd.h>

#include "lr_store.h"
#include "usb.h"
LOG_MODULE_REGISTER(usb, CONFIG_LOG_DEFAULT_LEVEL);

bool usb_charge = false;  // VBUS present (main.c blinks the green LED)

// USB (device_next stack): the CDC serial port (1200 bps touch, LR_LOG builds' log) and a card reader. The reader's
// disk "LR" has no medium until a phone lends it the SD card over BLE (lr_store.c, CTRL 0x06): the board then stops
// recording to the card and unmounts it, and the computer sees an ordinary USB disk, read-write. (Refusing writes hung
// macOS, 10-03: it mounts read-write and Spotlight retries.) Eject it on the computer before taking it back.
// lr_store.c keeps .metadata_never_index and .fseventsd/no_log on the card, so macOS writes next to nothing to it.
USBD_DEVICE_DEFINE(lr_usbd, DEVICE_DT_GET(DT_NODELABEL(zephyr_udc0)), 0x2fe3, 0x0100);
USBD_DESC_LANG_DEFINE(lr_lang);
USBD_DESC_MANUFACTURER_DEFINE(lr_mfr, "live-record");
USBD_DESC_PRODUCT_DEFINE(lr_product, CONFIG_LR_NAME_PREFIX " recorder");
USBD_DESC_SERIAL_NUMBER_DEFINE(lr_sn);
USBD_CONFIGURATION_DEFINE(lr_fs_config, 0, 250);
USBD_DEFINE_MSC_LUN(LR, "liverec", "SD card", "1.00");

volatile bool usb_disk_lent;

static int lr_disk_init(struct disk_info *disk)
{
    return 0;
}

static int lr_disk_status(struct disk_info *disk)
{
    return usb_disk_lent ? disk_access_status("SD") : DISK_STATUS_NOMEDIA;
}

static int lr_disk_read(struct disk_info *disk, uint8_t *buf, uint32_t sector, uint32_t count)
{
    return usb_disk_lent ? disk_access_read("SD", buf, sector, count) : -EIO;
}

static int lr_disk_write(struct disk_info *disk, const uint8_t *buf, uint32_t sector, uint32_t count)
{
    return usb_disk_lent ? disk_access_write("SD", buf, sector, count) : -EIO;
}

static int lr_disk_ioctl(struct disk_info *disk, uint8_t cmd, void *buf)
{
    if (!usb_disk_lent) {
        return -EIO;
    }
    return disk_access_ioctl("SD", cmd, buf);
}

static const struct disk_operations lr_disk_ops = {
    .init = lr_disk_init,
    .status = lr_disk_status,
    .read = lr_disk_read,
    .write = lr_disk_write,
    .ioctl = lr_disk_ioctl,
};
static struct disk_info lr_disk = {.name = "LR", .ops = &lr_disk_ops};

// Opening the USB serial port at 1200 baud reboots into the UF2 bootloader (the Arduino "1200 bps touch"),
// so a host flashes without anyone double-tapping reset: GPREGRET 0x57 is the Adafruit bootloader's UF2 magic.
static void to_bootloader(struct k_work *work)
{
    lr_store_shutdown();
    NRF_POWER->GPREGRET = 0x57;
    NVIC_SystemReset();
}
static K_WORK_DEFINE(to_bootloader_work, to_bootloader);

static bool usb_started;  // USB comes up 10 s after boot, Bluetooth first: a USB fault still leaves a way in
int16_t usb_diag[4];  // for the status read: setup error, last enable result, messages seen, last line rate / 100

static void on_msg(struct usbd_context *const ctx, const struct usbd_msg *msg)
{
    usb_diag[2]++;
    switch (msg->type) {
    case USBD_MSG_VBUS_READY:
        usb_charge = true;
        if (usb_started) {
            usb_diag[1] = usbd_enable(ctx);  // the nRF sees VBUS: the stack is enabled per plug-in
        }
        break;
    case USBD_MSG_VBUS_REMOVED:
        usb_charge = false;
        usbd_disable(ctx);
        lr_store_lend(false);  // unplugged: the card comes back to the recorder
        break;
    case USBD_MSG_CDC_ACM_LINE_CODING: {
        uint32_t rate = 0;
        uart_line_ctrl_get(msg->dev, UART_LINE_CTRL_BAUD_RATE, &rate);
        usb_diag[3] = rate / 100;
        if (rate == 1200) {
            k_work_submit(&to_bootloader_work);  // off the USB stack's context: the shutdown takes a mutex
        }
        break;
    }
    default:
        break;
    }
}

static void start_usb(struct k_work *work)
{
    usb_started = true;
    // plugged in before: no VBUS_READY comes for that, so enable now
    if (!usbd_can_detect_vbus(&lr_usbd) || nrf_power_usbregstatus_vbusdet_get(NRF_POWER)) {
        usb_diag[1] = usbd_enable(&lr_usbd);
    } else {
        usb_diag[1] = 1;  // not plugged in
    }
}
static K_WORK_DELAYABLE_DEFINE(usb_start, start_usb);

int init_usb()
{
    int err = disk_access_register(&lr_disk);
    err = err ?: usbd_add_descriptor(&lr_usbd, &lr_lang);
    err = err ?: usbd_add_descriptor(&lr_usbd, &lr_mfr);
    err = err ?: usbd_add_descriptor(&lr_usbd, &lr_product);
    err = err ?: usbd_add_descriptor(&lr_usbd, &lr_sn);
    err = err ?: usbd_add_configuration(&lr_usbd, USBD_SPEED_FS, &lr_fs_config);
    STRUCT_SECTION_FOREACH_ALTERNATE(usbd_class_fs, usbd_class_node, c) {
        err = err ?: usbd_register_class(&lr_usbd, c->c_data->name, USBD_SPEED_FS, 1);  // CDC ACM and MSC
    }
    if (err) {
        LOG_ERR("USB setup failed: %d", err);
        return err;
    }
    usbd_device_set_code_triple(&lr_usbd, USBD_SPEED_FS, USB_BCC_MISCELLANEOUS, 0x02, 0x01);  // CDC's IAD
    usbd_msg_register_cb(&lr_usbd, on_msg);
    err = usbd_init(&lr_usbd);
    usb_charge = nrf_power_usbregstatus_vbusdet_get(NRF_POWER);
    usb_diag[0] = err;
    LOG_INF("USB: %d", err);
    if (!err) {
        k_work_schedule(&usb_start, K_SECONDS(10));
    }
    return err;
}
