// Host check of src/led_code.h: cc -o led_check test/led_check.c && ./led_check
#include <assert.h>
#include <stdio.h>
#include "../src/led_code.h"

static int in_3s(bool bound, bool quiet, bool card, bool connected) // flashes over 6 calls (3 s)
{
    int k = 0;
    for (unsigned n = 0; n < 6; n++) {
        k += led_flashes(n, bound, quiet, card, connected);
    }
    return k;
}

int main(void)
{
    assert(in_3s(true, false, true, true) == 1);    // recording, phone connected
    assert(in_3s(true, false, true, false) == 2);   // recording, card only
    // the card before the link, the 3 together
    assert(in_3s(true, false, false, true) == 3 && in_3s(true, false, false, false) == 3);
    assert(led_flashes(0, true, false, false, true) == 3 && led_flashes(1, true, false, false, true) == 0);
    // unbound before the card: 1 a second either way
    assert(in_3s(false, false, true, false) == 3 && in_3s(false, false, false, false) == 3);
    assert(led_flashes(0, false, false, false, false) == 1 && led_flashes(2, false, false, false, false) == 1);
    assert(in_3s(true, true, true, true) == 0 && in_3s(false, true, false, false) == 0);  // paused / off
    assert(led_flashes(6, true, false, true, false) == 2 && led_flashes(7, true, false, true, false) == 0);  // per 3 s
    puts("led_check ok");
    return 0;
}
