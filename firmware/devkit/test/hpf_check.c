// Host check of src/hpf.h: cc -O2 -o hpf_check test/hpf_check.c -lm && ./hpf_check
// 6 Hz at -6 dBFS + 1 kHz, fed in 160-sample frames like the codec thread; levels over the last second.
#include <assert.h>
#include <stdio.h>
#include "../src/hpf.h"

#define FS 16000
#define N (3 * FS)

static double level(const int16_t *x, double f) // amplitude of the f Hz component over the last second
{
    double re = 0, im = 0;
    for (int k = N - FS; k < N; k++) {
        re += x[k] * cos(2 * M_PI * f * k / FS);
        im += x[k] * sin(2 * M_PI * f * k / FS);
    }
    return 2 * sqrt(re * re + im * im) / FS;
}

static double gain_db(double f, double amp, int16_t *in, int16_t *out, double f2, double amp2)
{
    for (int k = 0; k < N; k++)
        in[k] = out[k] = (int16_t) lrint(amp * sin(2 * M_PI * f * k / FS) + amp2 * sin(2 * M_PI * f2 * k / FS));
    struct hpf s[2];
    hpf_init(s, 80, FS);
    for (int k = 0; k < N; k += 160)
        hpf_run(s, out + k, 160);
    return 20 * log10(level(out, f) / level(in, f));
}

int main(void)
{
    static int16_t in[N], out[N];
    double lo = gain_db(6, 16384, in, out, 1000, 4000);
    double hi = 20 * log10(level(out, 1000) / level(in, 1000));
    double cut = gain_db(80, 10000, in, out, 0, 0);
    printf("6 Hz: %.1f dB, 1 kHz: %.3f dB, 80 Hz: %.2f dB\n", lo, hi, cut);
    assert(lo < -30 && fabs(hi) < 1 && fabs(cut + 3.01) < 0.2);
    puts("ok");
    return 0;
}
