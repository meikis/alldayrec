#ifndef SDCARD_H
#define SDCARD_H

#include <stdbool.h>
#include <stdint.h>

/**
 * @brief Mount the SD Card (FAT, formatted beforehand) at /SD:
 *
 * @return 0 if successful, negative errno code if error
 */
int mount_sd_card(void);
void unmount_sd_card(void);

void sd_on();
void sd_off();

bool is_sd_on();
#endif
