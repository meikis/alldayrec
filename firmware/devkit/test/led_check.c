// Host check of src/led_code.h: cc -o led_check test/led_check.c && ./led_check
#include <assert.h>
#include <stdio.h>
#include "../src/led_code.h"

static int in_10s(bool bound, bool quiet, bool connected) // flashes over 20 calls (10 s)
{
    int k = 0;
    for (unsigned n = 0; n < 20; n++) {
        k += led_flashes(n, bound, quiet, connected);
    }
    return k;
}

int main(void)
{
    assert(in_10s(true, false, true) == 1);    // recording, phone connected
    assert(in_10s(true, false, false) == 2);   // recording, card only
    assert(in_10s(false, false, false) == 10); // no phone bound: once a second
    assert(in_10s(true, true, true) == 0 && in_10s(false, true, false) == 0);  // paused / off
    assert(led_flashes(0, true, false, false) == 2 && led_flashes(1, true, false, false) == 0);  // the 2 come together
    puts("led_check ok");
    return 0;
}
