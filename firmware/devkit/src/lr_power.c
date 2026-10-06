/* LR-P3's power trail (CONFIG_LR_POWER_TRACE, #137 #122): on battery a POWER record with VDDH every minute, and the
 * chip's power-fail comparator's warning (VDDH under 3.3 V or VDD under 2.8 V) as it comes, each written to flash at
 * once (lr_log.c URGENT). A board that loses all power (a battery protection cutting out) keeps its last minute and
 * the voltage then; a supply that sags on the way down leaves the warning too. */
#include "lr_power.h"

#include <hal/nrf_power.h>
#include <nrfx_power.h>
#include <zephyr/kernel.h>

#include "lib/battery/battery.h"
#include "lr_log.h"

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
    lr_log(LR_POWER, 0, 1, 0);
}

static void minute(struct k_work *w)
{
    if (!nrf_power_usbregstatus_vbusdet_get(NRF_POWER)) {  // on USB VDDH is the cable's, not the battery's
        uint16_t mv = 0;
        battery_get_millivolt(&mv);
        lr_log(LR_POWER, MIN(mv / 20, 255), 0, 0);
    }
    nrfx_power_pof_enable(&pof);
    k_work_reschedule(k_work_delayable_from_work(w), K_MINUTES(1));
}
static K_WORK_DELAYABLE_DEFINE(minute_work, minute);

void lr_power_init(void)
{
    nrfx_power_pof_init(&pof);
    nrfx_power_pof_enable(&pof);
    k_work_schedule(&minute_work, K_MINUTES(1));
}
