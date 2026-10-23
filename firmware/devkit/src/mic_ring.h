#ifndef MIC_RING_H
#define MIC_RING_H
// The mic's 10 ms buffers on their way to the codec, each with its number (codec.c). One buffer is one codec frame.
// The PDM interrupt puts, the codec thread gets (one of each: no lock). A buffer the ring has no room for (the codec
// thread got no CPU for a second: a cooperative thread reading the card for a computer) is left out but its
// number is used, so the codec numbers its frames on past it: a gap the store counts (DROPPED, HEARTBEAT b), not a
// shift of every later frame. Host check: test/mic_ring_check.c
#include <stdbool.h>
#include <stdint.h>
#include <string.h>

#define MIC_RING_SAMPLES 160  // a buffer: 10 ms at 16 kHz, one codec frame
#define MIC_RING_SLOTS 100    // 1 s

struct mic_ring {
    int16_t pcm[MIC_RING_SLOTS][MIC_RING_SAMPLES];
    uint32_t no[MIC_RING_SLOTS];
    uint32_t put, got;  // buffers in and out, ever (wrap with uint32)
    uint32_t made;      // buffers the mic has given, kept or not (the interrupt's)
    uint32_t next;      // the number the codec expects next (the thread's)
};

// The PDM interrupt: a buffer in, numbered; false when the ring is full (left out)
static inline bool mic_ring_put(struct mic_ring *r, const int16_t *pcm)
{
    uint32_t n = r->made++;
    uint32_t put = r->put;
    if (put - __atomic_load_n(&r->got, __ATOMIC_ACQUIRE) == MIC_RING_SLOTS) {
        return false;
    }
    memcpy(r->pcm[put % MIC_RING_SLOTS], pcm, sizeof(r->pcm[0]));
    r->no[put % MIC_RING_SLOTS] = n;
    __atomic_store_n(&r->put, put + 1, __ATOMIC_RELEASE);
    return true;
}

// The codec thread: the oldest buffer out and how many were left out just before it; false when there is none
static inline bool mic_ring_get(struct mic_ring *r, int16_t *pcm, uint32_t *left_out)
{
    uint32_t got = r->got;
    if (__atomic_load_n(&r->put, __ATOMIC_ACQUIRE) == got) {
        return false;
    }
    memcpy(pcm, r->pcm[got % MIC_RING_SLOTS], sizeof(r->pcm[0]));
    uint32_t n = r->no[got % MIC_RING_SLOTS];
    *left_out = n - r->next;
    r->next = n + 1;
    __atomic_store_n(&r->got, got + 1, __ATOMIC_RELEASE);
    return true;
}

#endif
