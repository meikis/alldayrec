#include "sdcard.h"
#include "lr_log.h"
#include "wdog_facade.h"

#include <ff.h>
#include <zephyr/device.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/drivers/spi.h>
#include <zephyr/fs/fs.h>
#include <zephyr/fs/fs_sys.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/storage/disk_access.h>
#include <zephyr/sys/check.h>

LOG_MODULE_REGISTER(sdcard, CONFIG_LOG_DEFAULT_LEVEL);

static FATFS fat_fs;

static struct fs_mount_t mount_point = {
    .type = FS_FATFS,
    .fs_data = &fat_fs,
};

static bool sd_enabled;

#if DT_NODE_HAS_PROP(DT_PATH(zephyr_user), sd_pwr_gpios)
static const struct gpio_dt_spec sd_pwr = GPIO_DT_SPEC_GET(DT_PATH(zephyr_user), sd_pwr_gpios);
#endif

// the card's chip select, as the board overlay gives it to the SD SPI bus (LR-X1 D10/P1.15, LR-P1 P0.20)
static const struct gpio_dt_spec sd_cs = GPIO_DT_SPEC_GET_BY_IDX(DT_NODELABEL(spi2), cs_gpios, 0);

// no cs in the config: CS is driven by hand, so it stays high for the wake-up clocks. 400 kHz, as card init wants
static const struct spi_config sd_reset_cfg = {.frequency = 400000,
                                               .operation = SPI_OP_MODE_MASTER | SPI_WORD_SET(8) | SPI_TRANSFER_MSB};

static uint8_t sd_byte(uint8_t tx)
{
    uint8_t rx = 0xFF;
    const struct spi_buf tb = {.buf = &tx, .len = 1}, rb = {.buf = &rx, .len = 1};
    const struct spi_buf_set ts = {.buffers = &tb, .count = 1}, rs = {.buffers = &rb, .count = 1};
    spi_transceive(DEVICE_DT_GET(DT_NODELABEL(spi2)), &sd_reset_cfg, &ts, &rs);
    return rx;
}

static uint8_t sd_cmd(const uint8_t cmd[6], int skip)
{
    for (int i = 0; i < 6; i++) {
        sd_byte(cmd[i]);
    }
    uint8_t r1 = 0xFF;
    for (int i = 0; i < skip + 8 && (i < skip || r1 & 0x80); i++) {
        r1 = sd_byte(0xFF);
    }
    return r1;
}

// A card never powered off (the XIAO's module sits on 3V3) can be left mid-read by a warm reset, streaming data and
// deaf to the CMD0 sd_init starts with. The driver's 80 wake-up clocks do not help: they go out with CS low
// (spi_context ignores SPI_CS_ACTIVE_HIGH for a GPIO CS), and nothing in it sends CMD12. Leaves CS high and the bus
// unlocked, as sdhc_spi expects.
static void sd_spi_reset(void)
{
    static const uint8_t cmd12[6] = {0x4C, 0, 0, 0, 0, 0x61}, cmd0[6] = {0x40, 0, 0, 0, 0, 0x95};
    gpio_pin_configure_dt(&sd_cs, GPIO_OUTPUT_INACTIVE);
    for (int i = 0; i < 10; i++) {
        sd_byte(0xFF);
    }
    gpio_pin_set_dt(&sd_cs, 1);
    uint8_t r12 = sd_cmd(cmd12, 1);  // its response comes after a stuff byte
    for (int i = 0; i < 20000 && sd_byte(0xFF) != 0xFF; i++) {  // busy until the read has stopped; ~0.5 s at most
    }
    uint8_t r0 = sd_cmd(cmd0, 0);
    gpio_pin_set_dt(&sd_cs, 0);
    sd_byte(0xFF);
    LOG_INF("SD reset: CMD12 r1 0x%02x, CMD0 r1 0x%02x", r12, r0);
    lr_log(LR_SD_RESET, 0, r12, r0);
}

int mount_sd_card(void)
{
    // at boot only: NCS 2.7's SD disk has no deinit, it would not initialise the card again (mounted back after USB, -EIO)
    static bool booted;
#if DT_NODE_HAS_PROP(DT_PATH(zephyr_user), sd_pwr_gpios)
    // a board that switches the card's supply: off long enough to drain, then on, so the card starts from reset
    if (!booted) {
        gpio_pin_configure_dt(&sd_pwr, GPIO_OUTPUT_INACTIVE);
        k_msleep(300);
        gpio_pin_set_dt(&sd_pwr, 1);
        k_msleep(50);
    }
#endif
    if (!booted) {
        sd_spi_reset();
        booted = true;
    }

    const char *disk_pdrv = "SD";
    // after a warm reset the card (never powered off) may still be mid-command: give it a few tries
    mount_point.mnt_point = "/SD:";
    mount_point.flags = FS_MOUNT_FLAG_NO_FORMAT;  // a card that reads badly once must not be formatted, recordings and all
    int err = -1;
    for (int i = 0; i < 5 && err; i++) {
        if (i) {
            k_msleep(200 * i);
        }
        watchdog_feed();  // a card that never answers takes seconds per try; the watchdog allows 30 s for all of boot
        err = disk_access_init(disk_pdrv) ?: fs_mount(&mount_point);
        LOG_INF("SD mount try %d: %d", i + 1, err);
        lr_log(LR_MOUNT, i + 1, err, 0);
    }
    sd_enabled = !err;
    return err;
}

/* One try, no waiting: the store tries a failed card again once a minute (lr_store.c). Logged when it works. */
int mount_sd_card_once(void)
{
    int err = disk_access_init("SD") ?: fs_mount(&mount_point);
    if (!err) {
        lr_log(LR_MOUNT, 0, 0, 0);
    }
    sd_enabled = !err;
    return err;
}

void unmount_sd_card(void)
{
    fs_unmount(&mount_point);
    sd_enabled = false;
}

/* Mount and unmount only: the Omi board's SD enable pin (P0.19) is not routed on LR-X1 or LR-P1. LR-X1 feeds the
 * card from 3V3; LR-P1 from the nice!nano VCC (sd-pwr-gpios), cycled once at boot above. */
void sd_off()
{
    if (sd_enabled) {
        unmount_sd_card();
    }
}

void sd_on()
{
    if (!sd_enabled) {
        mount_sd_card();
    }
}

bool is_sd_on()
{
    return sd_enabled;
}
