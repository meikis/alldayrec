// Host check of lr_low_count (src/lr_power.h), what turns a recorder off: cc -o power_check test/power_check.c && ./power_check
#include <assert.h>
#include <stdio.h>
#include "../src/lr_power.h"

// readings a minute apart -> the minute the third low one in a row comes (1-based), 0: never
static int off_at(const uint16_t *mv, int n, bool usb, int err, uint16_t limit)
{
    for (int i = 0, low = 0; i < n; i++) {
        if ((low = lr_low_count(low, usb, err, mv[i], limit)) >= 3) {
            return i + 1;
        }
    }
    return 0;
}

int main(void)
{
    const uint16_t fall[] = {3400, 3320, 3290, 3280, 3260, 3240}, blip[] = {3290, 3290, 3310, 3290, 3290, 3310};
    const uint16_t misread[] = {0, 0, 0, 1500, 1500, 1500};
    assert(off_at(fall, 6, false, 0, 3300) == 5);   // three in a row under 3.3 V
    assert(off_at(blip, 6, false, 0, 3300) == 0);   // one over starts the count again
    assert(off_at(fall, 6, true, 0, 3300) == 0);    // on USB: the cable's voltage
    assert(off_at(fall, 6, false, -5, 3300) == 0);  // the ADC failed
    assert(off_at(misread, 6, false, 0, 3300) == 0);  // no board runs at that: a misread
    assert(off_at(fall, 6, false, 0, 0) == 0);      // CONFIG_LR_LOW_BATTERY_MV 0: never
    puts("power_check ok");
    return 0;
}
