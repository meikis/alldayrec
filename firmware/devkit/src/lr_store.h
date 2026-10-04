#ifndef LR_STORE_H
#define LR_STORE_H

#include <stdbool.h>
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
 *               (0: storing; else -errno: -19 no card, -28 card full, -16 lent to USB) free_kb:u32 total_kb:u32
 *               resetreas:u32 (the last reset's cause) fault_reason:u32 fault_pc:u32 (a crash before it; 0: none)
 *               dropped:u32 (frames that never reached the card this boot) append_max_ms:u16 (slowest card write)
 *               usb_diag:4 x i16 bonds:u8 (phones bonded; 0: unbound, not recording) pause_left_s:u32 (0: recording)
 *   ...02 CTRL  write commands, notify replies:
 *               0x01 LIST                -> 0x81 file:u32 size:u32 per closed file, then 0x80 err:i8; closes the open segment
 *                                           first, so a phone that takes the files before it listens leaves none behind
 *               0x02 READ file:u32 offset:u32 -> DATA notifications, then 0x82 file:u32 end:u32
 *               0x03 STOP
 *               0x04 DELETE file:u32     -> 0x84 file:u32 status:i8 (the open file is refused: -16)
 *               0x05 FORGET              -> 0x85 n:u32: every bonded phone but this one is forgotten
 *               0x05 0x01 FORGET all     -> 0x85 n:u32, then this phone is forgotten too and the link goes (unbinding)
 *               0x06 USB_DISK on:u8      -> 0x86 on:u32 status:i8: on a computer's USB, the card shows as a USB disk
 *                                           (recording to it pauses, TIME's store reads -16) or comes back (eject
 *                                           it on the computer first); unplugging also brings it back. -107: not on USB
 *               0x07 LOG from:u32        -> DATA notifications seq:u32 + records (seq, seq+1, ...; 16 B each, lr_log.h)
 *                                           for every logged event from seq `from` on, then 0x87 next:u32, the seq
 *                                           the next event will get: ask from there next time
 *               0x20 PAUSE s:u32         nothing kept for s seconds (frames still counted), then recording again by
 *                                           itself; 0 resumes now; a reset resumes too. No reply: the write's response
 * All but battery and device info need an encrypted (bonded) link; see pairing in transport.c. Nothing is recorded
 * while no phone is bonded (transport.c). The scan response's manufacturer data (0xFFFF) carries the state byte.
 *   ...03 DATA  notify offset:u32 bytes...
 * file = boot << 16 | segment.
 */

int lr_store_init(void);
/* One encoded frame, numbered by the codec since boot; called with the SD mutex held. after_gap: frames before it were
 * left out on purpose (unbound, paused), not lost. */
void lr_store_frame(uint32_t frame_no, const uint8_t *data, uint16_t len, bool after_gap);
/* Close the open segment (a phone connected): what is recorded so far becomes readable. */
void lr_store_rotate(void);
/* USB unplugged: take the card back if it was lent (usb.c). */
void lr_store_lend(bool on);

/* Before a reset: write out the open record and unmount. A card reset mid-command stays dead until power-off. */
void lr_store_shutdown(void);

#endif
