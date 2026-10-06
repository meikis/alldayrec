#ifndef LR_POWER_H
#define LR_POWER_H

#include <stdbool.h>
#include <stdint.h>

/* What a POWER event is (its a, lr_log.h) */
enum { LR_POWER_MINUTE, LR_POWER_WARNING, LR_POWER_LOW };

/* After lr_log_init, before the card and the radio: the restart after a low battery goes to System OFF here; else
 * the battery is read once a minute from now on (lr_power.c) */
void lr_power_init(void);
/* The open file written out and closed, the phone told: before a restart that stops recording */
void lr_power_stop(void);
/* The log to flash, then System OFF with the card's and the mic's supplies off; USB (VBUS) wakes it, and LR-P3's
 * switch moving. Does not return. */
void lr_power_off(void);

/* A minute's battery reading: how many in a row are under limit now. On USB (the reading is the cable's), a failed
 * read, or one the board could not run at (a misread, not an empty battery): none. */
static inline int lr_low_count(int count, bool usb, int err, uint16_t mv, uint16_t limit)
{
    return !usb && !err && mv > 2000 && mv < limit ? count + 1 : 0;
}

#endif
