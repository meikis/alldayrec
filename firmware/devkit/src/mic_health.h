#pragma once
// The mic over the last 10 minutes, for the event log's HEALTH (lr_log.h, #155 F1): the codec thread counts each
// 10 ms frame, lr_log.c packs the counts and clears them every 10 minutes.
// ponytail: plain counters, written by the codec thread and cleared by the system workqueue: an increment may be lost
// at the clearing; fine for counts of 10 minutes
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>

struct mic_health {
    uint32_t zero_frames, full;  // 10 ms frames all zero, samples at full scale: before the high-pass
    uint16_t loudest;            // after it (the PDM path carries 2-20 Hz at up to -8 dBFS before)
};

/* A frame from the mic, before the high-pass */
static inline void mic_health_raw(struct mic_health *h, const int16_t *s, int n)
{
    bool zero = true;
    for (int i = 0; i < n; i++) {
        zero = zero && !s[i];
        h->full += s[i] == INT16_MAX || s[i] == INT16_MIN;
    }
    h->zero_frames += zero;
}

/* The same frame after the high-pass */
static inline void mic_health_out(struct mic_health *h, const int16_t *s, int n)
{
    for (int i = 0; i < n; i++) {
        uint16_t v = abs(s[i]);
        h->loudest = v > h->loudest ? v : h->loudest;
    }
}

/* HEALTH's a and b: ms of all-zero frames | PDM errors << 20; samples at full scale | the loudest >> 7 << 24. Every
 * field saturates */
static inline uint32_t mic_health_a(const struct mic_health *h, uint32_t pdm_errors)
{
    uint32_t ms = h->zero_frames * 10;
    return (ms < 0xFFFFF ? ms : 0xFFFFF) | (pdm_errors < 0xFFF ? pdm_errors : 0xFFF) << 20;
}

static inline uint32_t mic_health_b(const struct mic_health *h)
{
    uint32_t peak = h->loudest >> 7;
    return (h->full < 0xFFFFFF ? h->full : 0xFFFFFF) | (peak < 0xFF ? peak : 0xFF) << 24;
}
