#ifndef LED_CODE_H
#define LED_CODE_H

#include <stdbool.h>

/* A one-LED board's status (CONFIG_LR_ONE_LED, the ProMicro's P0.15) as short flashes, never lit steadily: how many
 * to flash at call n (main.c calls every 500 ms, so 20 calls are 10 s). Recording with a phone connected: 1 every
 * 10 s; recording to the card only: 2 every 10 s; no phone bound: 1 a second; paused or off: none. */
static inline int led_flashes(unsigned n, bool bound, bool quiet, bool connected)
{
    if (quiet) {
        return 0;
    }
    if (!bound) {
        return n % 2 == 0;
    }
    return n % 20 ? 0 : connected ? 1 : 2;
}

#endif
