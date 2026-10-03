#include "sdcard.h"
#include "wdog_facade.h"

#include <ff.h>
#include <zephyr/device.h>
#include <zephyr/drivers/gpio.h>
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

int mount_sd_card(void)
{
#if DT_NODE_HAS_PROP(DT_PATH(zephyr_user), sd_pwr_gpios)
    // a board that switches the card's supply: off long enough to drain, then on, so the card starts from reset. At
    // boot only: NCS 2.7's SD disk has no deinit, it would not initialise the card again (mounted back after USB, -EIO)
    static bool powered;
    if (!powered) {
        gpio_pin_configure_dt(&sd_pwr, GPIO_OUTPUT_INACTIVE);
        k_msleep(300);
        gpio_pin_set_dt(&sd_pwr, 1);
        k_msleep(50);
        powered = true;
    }
#endif
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
