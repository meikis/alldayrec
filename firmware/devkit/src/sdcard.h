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
int mount_sd_card_once(void);
void unmount_sd_card(void);

void sd_on();
void sd_off();

bool is_sd_on();

/* The card's CID and CSD as its last init read them (sdhc_spi.c takes CMD10's and CMD9's data blocks): CARD_ID and
 * CARD_SIZE at each mount (#155 F2) */
extern uint8_t lr_sd_cid[16], lr_sd_csd[16];
/* The SD driver's failed tries of data transfers (sdhc_spi.c), read and cleared with SD_WRITES (lr_store.c), both under
 * the SD mutex: SD_ERRORS (#155 F3) */
enum { LR_SD_CRC, LR_SD_TIMEOUT, LR_SD_BUSY, LR_SD_OTHER, LR_SD_RETRIED, LR_SD_ERR_N };
extern uint32_t lr_sd_err[LR_SD_ERR_N];
#endif
