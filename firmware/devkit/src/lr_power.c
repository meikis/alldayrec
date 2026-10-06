/* Power (#95 #122 #137). On battery the battery is read once a minute; three readings in a row under
 * CONFIG_LR_LOW_BATTERY_MV end recording while the cell still holds the board up: a POWER event, the open file closed,
 * the phone told, and a restart into System OFF until USB (VBUS) comes, or LR-P3's switch moves. Without it the X1 ran
 * to ~3.0 V and browned out over and over, a card mount each time; at 3.3 V there were still 25-29 minutes left (#95).
 *
 * LR-P3 (CONFIG_LR_POWER_TRACE) also keeps a power trail: a POWER record with VDDH every minute, and the chip's
 * power-fail warning (VDDH under 3.3 V or VDD under 2.8 V) as it comes, each written to flash at once (lr_log.c
 * URGENT). A board that loses all power (a battery protection cutting out) keeps its last minute and the voltage. */
#include "lr_power.h"

#include <hal/nrf_gpio.h>
#include <hal/nrf_power.h>
#include <nrfx_power.h>
#include <zephyr/bluetooth/conn.h>
#include <zephyr/bluetooth/hci.h>
#include <zephyr/kernel.h>

#include "config.h"
#include "lib/battery/battery.h"
#include "lr_log.h"
#include "lr_store.h"
#include "lr_switch.h"
#include "transport.h"

#define LOW_READINGS 3
#define OFF_MAGIC 0x10B477E7
static __noinit uint32_t off_at_boot;  // OFF_MAGIC: the restart was a low battery's (a cold start's RAM fails it)

#ifdef CONFIG_LR_POWER_TRACE
static void warn(void);
static const nrfx_power_pofwarn_config_t pof = {
    .handler = warn,
    .thr = NRF_POWER_POFTHR_V28,
    .thrvddh = NRF_POWER_POFTHRVDDH_V33,
};

/* In the POWER interrupt: no ADC here (VDDH 0, not measured); once, the minute arms it again */
static void warn(void)
{
    nrfx_power_pof_disable();
    lr_log(LR_POWER, 0, LR_POWER_WARNING, 0);
}
#endif

void lr_power_stop(void)
{
    lr_store_shutdown();
    struct bt_conn *conn = get_current_connection();
    if (conn) {
        bt_conn_disconnect(conn, BT_HCI_ERR_REMOTE_POWER_OFF);
        k_msleep(200);  // the disconnect goes out before the radio stops
    }
}

/* At boot, or up on USB with the cable gone (lr_switch.c): the radio and the mic's EasyDMA not started, as System OFF
 * wants. The log to flash first (System OFF keeps no RAM); outputs keep their level. */
void lr_power_off(void)
{
    lr_log_sync();
#if DT_NODE_HAS_PROP(LR_USER, sd_pwr_gpios)
    nrf_gpio_pin_clear(LR_PIN(sd_pwr_gpios));
    nrf_gpio_cfg_output(LR_PIN(sd_pwr_gpios));
#endif
    nrf_gpio_pin_clear(PDM_PWR_PIN);
    nrf_gpio_cfg_output(PDM_PWR_PIN);
    battery_charge_stop();  // the XIAO's P0.17, the charger's ~CHG, driven low keeps its red LED lit (#95)
#ifdef CONFIG_LR_SWITCH
    lr_switch_arm();
#endif
    NRF_POWER->SYSTEMOFF = 1;  // VBUS rising wakes it too
    for (;;) {
    }
}

static void minute(struct k_work *w)
{
    static int low;
    bool usb = nrf_power_usbregstatus_vbusdet_get(NRF_POWER);  // on USB the reading is the cable's, not the battery's
    uint16_t mv = 0;
    int err = usb ? 0 : battery_get_millivolt(&mv);
#ifdef CONFIG_LR_POWER_TRACE
    if (!usb) {
        lr_log(LR_POWER, MIN(mv / 20, 255), LR_POWER_MINUTE, 0);
    }
    nrfx_power_pof_enable(&pof);
#endif
    low = lr_low_count(low, usb, err, mv, CONFIG_LR_LOW_BATTERY_MV);
    if (low >= LOW_READINGS) {
        lr_log(LR_POWER, MIN(mv / 20, 255), LR_POWER_LOW, 0);
        lr_power_stop();
        off_at_boot = OFF_MAGIC;
        lr_log_sync();
        NVIC_SystemReset();
    }
    k_work_reschedule(k_work_delayable_from_work(w), K_MINUTES(1));
}
static K_WORK_DELAYABLE_DEFINE(minute_work, minute);

void lr_power_init(void)
{
    bool off = off_at_boot == OFF_MAGIC;
    off_at_boot = 0;
    if (off && !nrf_power_usbregstatus_vbusdet_get(NRF_POWER)) {  // plugged in meanwhile: no VBUS rising would wake it
        lr_power_off();
    }
#ifdef CONFIG_LR_POWER_TRACE
    nrfx_power_pof_init(&pof);
    nrfx_power_pof_enable(&pof);
#endif
    k_work_schedule(&minute_work, K_MINUTES(1));
}
