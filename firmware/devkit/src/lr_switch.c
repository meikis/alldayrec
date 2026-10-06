/* LR-P3's slide switch (#122), CONFIG_LR_SWITCH: P1.15 on its pull-up, closed to GND = on. Off ends the open file,
 * drops the phone and restarts; a boot with the switch off goes to System OFF at once, before the card or the radio,
 * and wakes when it is switched on (P1.15 low) or USB comes (VBUS). On USB it stays up instead, not recording (the
 * USB port still takes a firmware), until the switch is on (restart, recording) or the cable goes (System OFF).
 * Events: SWITCH (lr_log.h); the wake reason is BOOT's RESETREAS (OFF: the switch, VBUS: USB). */
#include "lr_switch.h"

#include <hal/nrf_gpio.h>
#include <hal/nrf_power.h>
#include <zephyr/kernel.h>

#include "lr_log.h"
#include "lr_power.h"
#include "wdog_facade.h"

#define SWITCH NRF_GPIO_PIN_MAP(1, 15)  // the other ProMicros leave it open: it would read off, hence the Kconfig
#define SETTLE 3                        // the same reading this many polls in a row (100 ms apart) counts

static bool on(void)
{
    return !nrf_gpio_pin_read(SWITCH);
}

static bool vbus(void)
{
    return nrf_power_usbregstatus_vbusdet_get(NRF_POWER);
}

static void restart(bool now_on, uint32_t then)
{
    lr_log(LR_SWITCH, now_on, then, 0);
    lr_log_sync();
    NVIC_SystemReset();
}

static void power_off(void)
{
    lr_log(LR_SWITCH, 0, LR_SWITCH_SYSTEM_OFF, 0);
    lr_power_off();
}

void lr_switch_arm(void)
{
    nrf_gpio_cfg_input(SWITCH, NRF_GPIO_PIN_PULLUP);
    k_msleep(1);
    nrf_gpio_cfg_sense_input(SWITCH, NRF_GPIO_PIN_PULLUP, on() ? NRF_GPIO_PIN_SENSE_HIGH : NRF_GPIO_PIN_SENSE_LOW);
}

/* Running: switched off, the open file closed and the phone told before the restart that decides what comes next */
static void poll(struct k_work *w)
{
    static int offs;
    offs = on() ? 0 : offs + 1;
    if (offs < SETTLE) {
        k_work_reschedule(k_work_delayable_from_work(w), K_MSEC(100));
        return;
    }
    lr_power_stop();
    restart(false, LR_SWITCH_RESTART);
}
static K_WORK_DELAYABLE_DEFINE(poll_work, poll);

bool lr_switch_boot(void)
{
    nrf_gpio_cfg_input(SWITCH, NRF_GPIO_PIN_PULLUP);
    k_msleep(1);
    int offs = 0;
    for (int i = 0; i < SETTLE; i++, k_msleep(10)) {
        offs += !on();
    }
    if (offs < SETTLE) {  // on, or bouncing: recording, and the poll sees it settle
        lr_log(LR_SWITCH, 1, LR_SWITCH_RECORDING, 0);
        k_work_schedule(&poll_work, K_MSEC(100));
        return true;
    }
    if (!vbus()) {
        power_off();
    }
    lr_log(LR_SWITCH, 0, LR_SWITCH_PARKED, 0);
    return false;
}

void lr_switch_park(void)
{
    int ons = 0, gone = 0;
    for (;;) {
        k_msleep(100);
        watchdog_feed();
        ons = on() ? ons + 1 : 0;
        gone = vbus() ? 0 : gone + 1;
        if (ons >= SETTLE) {
            restart(true, LR_SWITCH_RESTART);
        }
        if (gone >= SETTLE) {
            power_off();
        }
    }
}
