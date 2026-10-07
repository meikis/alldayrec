#ifndef LED_CODE_H
#define LED_CODE_H

#include <stdbool.h>

/* A one-LED board's status (CONFIG_LR_ONE_LED, the ProMicro's P0.15) as 30 ms flashes 150 ms apart: how many to
 * flash at call n (main.c calls every 500 ms, so 6 calls are 3 s). The first that holds wins:
 *   paused, or off (going down): none;
 *   no phone bound: 1 a second;
 *   nothing going to the card (no card, full, a failed write, lent to USB): 3 every 3 s;
 *   recording, a phone connected: 1 every 3 s;
 *   recording to the card only: 2 every 3 s.
 * Besides (main.c, lr_switch.c): lit 1 s as recording starts (LR-P3: switched on), 3 quick ones when switched off. */
static inline int led_flashes(unsigned n, bool bound, bool quiet, bool card, bool connected)
{
    if (quiet) {
        return 0;
    }
    if (!bound) {
        return n % 2 == 0;
    }
    return n % 6 ? 0 : !card ? 3 : connected ? 1 : 2;
}

#endif
