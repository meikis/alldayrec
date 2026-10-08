// Host check of src/mic_health.h (#155 F1): cc -o health_check test/health_check.c && ./health_check
#include <assert.h>
#include <stdio.h>
#include "../src/mic_health.h"

int main(void)
{
    struct mic_health h = {0};
    int16_t f[160] = {0};
    mic_health_raw(&h, f, 160);  // all zero: 10 ms
    f[80] = 1;
    mic_health_raw(&h, f, 160);  // one sample is not zero: not counted
    f[0] = INT16_MAX, f[1] = INT16_MIN, f[2] = INT16_MAX - 1;
    mic_health_raw(&h, f, 160);  // two at full scale, either way
    assert(h.zero_frames == 1 && h.full == 2);
    mic_health_out(&h, f, 160);
    assert(h.loudest == 32768);  // INT16_MIN's
    assert(mic_health_a(&h, 3) == (10 | 3 << 20) && mic_health_b(&h) == (2 | 0xFFu << 24));
    f[0] = 1 << 14, f[1] = f[2] = 0;
    h.loudest = 0;
    mic_health_out(&h, f, 160);
    assert(mic_health_b(&h) >> 24 == 128);  // -6 dBFS
    // every field saturates: 10 minutes all zero is 600000 ms (fits), a stuck PDM can count more errors than 12 bits
    h.zero_frames = 200000, h.full = 1 << 25;
    assert(mic_health_a(&h, 5000) == (0xFFFFF | 0xFFFu << 20) && (mic_health_b(&h) & 0xFFFFFF) == 0xFFFFFF);
    printf("health_check ok\n");
    return 0;
}
