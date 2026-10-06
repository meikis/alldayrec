#ifndef LR_SWITCH_H
#define LR_SWITCH_H

#include <stdbool.h>

/* What follows a SWITCH event (its a) */
enum { LR_SWITCH_RECORDING, LR_SWITCH_SYSTEM_OFF, LR_SWITCH_PARKED, LR_SWITCH_RESTART };

/* At boot, after lr_log_init: true if on (recording goes ahead, the switch watched from now on); off without USB it
 * does not return (System OFF); off on USB, false: start USB, then lr_switch_park(). */
bool lr_switch_boot(void);
/* Up on USB, not recording, until the switch is on (restart) or the cable goes (System OFF). Does not return. */
void lr_switch_park(void);
/* Before System OFF (lr_power_off): the switch moving either way wakes it */
void lr_switch_arm(void);

#endif
