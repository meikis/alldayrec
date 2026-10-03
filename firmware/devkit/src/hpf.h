#pragma once
// 4th-order Butterworth high-pass on 16-bit PCM: two RBJ-cookbook biquads, float state kept across calls.
#include <math.h>
#include <stddef.h>
#include <stdint.h>

struct hpf {
    float b0, b1, a1, a2, x1, x2, y1, y2; // high-pass: b2 = b0
};

static inline void hpf_init(struct hpf s[2], float fc, float fs)
{
    static const float q[2] = {0.54119610f, 1.30656296f}; // Butterworth 4th order: 1 / (2 cos(k pi / 8)), k = 1, 3
    float w = 2.0f * 3.14159265f * fc / fs, c = cosf(w);
    for (int i = 0; i < 2; i++) {
        float alpha = sinf(w) / (2.0f * q[i]), a0 = 1.0f + alpha;
        s[i] = (struct hpf) {
            .b0 = (1.0f + c) / 2.0f / a0, .b1 = -(1.0f + c) / a0, .a1 = -2.0f * c / a0, .a2 = (1.0f - alpha) / a0};
    }
}

static inline void hpf_run(struct hpf s[2], int16_t *pcm, size_t n)
{
    for (size_t k = 0; k < n; k++) {
        float v = pcm[k];
        for (int i = 0; i < 2; i++) {
            struct hpf *f = &s[i];
            float y = f->b0 * (v + f->x2) + f->b1 * f->x1 - f->a1 * f->y1 - f->a2 * f->y2;
            f->x2 = f->x1;
            f->x1 = v;
            f->y2 = f->y1;
            f->y1 = y;
            v = y;
        }
        pcm[k] = v >= 32767.0f ? 32767 : v <= -32768.0f ? -32768 : (int16_t) lrintf(v);
    }
}
