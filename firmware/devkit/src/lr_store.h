#ifndef LR_STORE_H
#define LR_STORE_H

#include <stdint.h>

/*
 * live-record offline store: audio recorded while no phone listens, kept on the SD card with what is needed to put
 * it back on the wall clock later. The device has no clock; the mic does: frame n of a boot was captured 10 ms
 * after frame n-1, so a frame's time is its boot's sync record (frame f was epoch e) + (n - f) * 10 ms.
 *
 * Files: /SD:/LR/BBBBSSSS.BIN (hex boot number, segment). A new segment starts at each connection, so everything
 * recorded before it is in closed files a phone can read and delete. Records, little endian:
 *   'A' count:u8 first_frame:u32  then count x (len:u8 opus[len])   consecutive frames first_frame, +1, ...
 *   'T' frame:u32 epoch_ms:u64                                      the phone said frame `frame` was at epoch_ms
 * A file cut short by power loss ends in a partial record: readers stop there.
 *
 * GATT service 7c3e0000-1a2b-4c5d-8e9f-6c6976657265:
 *   ...01 TIME  write epoch_ms:u64 (phone's clock, on every connect); read -> boot:u16 frame:u32 (the frame now) store:i16
 *               (0: storing; else -errno, e.g. -19 no card)
 *   ...02 CTRL  write commands, notify replies:
 *               0x01 LIST                -> 0x81 file:u32 size:u32 per closed file, then 0x80
 *               0x02 READ file:u32 offset:u32 -> DATA notifications, then 0x82 file:u32 end:u32
 *               0x03 STOP
 *               0x04 DELETE file:u32     -> 0x84 file:u32 status:i8 (the open file is refused: -16)
 *   ...03 DATA  notify offset:u32 bytes...
 * file = boot << 16 | segment.
 */

int lr_store_init(void);
/* One encoded frame, numbered by the codec since boot; called with the SD mutex held. */
void lr_store_frame(uint32_t frame_no, const uint8_t *data, uint16_t len);
/* Close the open segment (a phone connected): what is recorded so far becomes readable. */
void lr_store_rotate(void);

/* Before a reset: write out the open record and unmount. A card reset mid-command stays dead until power-off. */
void lr_store_shutdown(void);

#endif
