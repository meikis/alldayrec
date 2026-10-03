#include "sdcard.h"
#include "wdog_facade.h"

#include <ff.h>
#include <zephyr/device.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/fs/fs.h>
#include <zephyr/fs/fs_sys.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/pm/device.h>
#include <zephyr/storage/disk_access.h>
#include <zephyr/sys/check.h>

LOG_MODULE_REGISTER(sdcard, CONFIG_LOG_DEFAULT_LEVEL);

static FATFS fat_fs;

static struct fs_mount_t mount_point = {
    .type = FS_FATFS,
    .fs_data = &fat_fs,
};

struct gpio_dt_spec sd_en_gpio_pin = {.port = DEVICE_DT_GET(DT_NODELABEL(gpio0)),
                                      .pin = 19,
                                      .dt_flags = GPIO_INT_DISABLE};



bool sd_enabled = false;
static const struct gpio_dt_spec sd_cs = GPIO_DT_SPEC_GET_BY_IDX(DT_NODELABEL(spi2), cs_gpios, 0);  // per board overlay

int mount_sd_card(void)
{
    // initialize the sd card enable pin (v2)
    if (gpio_is_ready_dt(&sd_en_gpio_pin)) {
        LOG_INF("SD Enable Pin ready");
    } else {
        LOG_ERR("Error setting up SD Enable Pin");
        return -1;
    }

    if (gpio_pin_configure_dt(&sd_en_gpio_pin, GPIO_OUTPUT_ACTIVE) < 0) {
        LOG_ERR("Error setting up SD Pin");
        return -1;
    }
    sd_enabled = true;

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
    return err;
}

void unmount_sd_card(void)
{
    fs_unmount(&mount_point);
}

void sd_off()
{
    // Suspend SPI peripheral to save power (its pins follow the overlay's pinctrl)
    const struct device *spi_dev = DEVICE_DT_GET(DT_NODELABEL(spi2));
    if (device_is_ready(spi_dev)) {
        pm_device_action_run(spi_dev, PM_DEVICE_ACTION_SUSPEND);
    }
    gpio_pin_configure_dt(&sd_cs, GPIO_DISCONNECTED);
    gpio_pin_set_dt(&sd_en_gpio_pin, 0);

    sd_enabled = false;
}

void sd_on()
{
    gpio_pin_set_dt(&sd_en_gpio_pin, 1);
    gpio_pin_configure_dt(&sd_cs, GPIO_OUTPUT_INACTIVE);
    const struct device *spi_dev = DEVICE_DT_GET(DT_NODELABEL(spi2));
    if (device_is_ready(spi_dev)) {
        pm_device_action_run(spi_dev, PM_DEVICE_ACTION_RESUME);
    }
    sd_enabled = true;
}

bool is_sd_on()
{
    return sd_enabled;
}
