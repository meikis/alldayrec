// Host check of LR-P3's switch debounce (src/lr_switch.h): cc -o switch_check test/switch_check.c && ./switch_check
#include <assert.h>
#include <stdio.h>
#include "../src/lr_switch.h"

// reads of one position every LR_SWITCH_POLL_MS for ms -> what the last read says
static int hold(bool on, int ms, bool *last, int *n)
{
    int r = -1;
    for (int t = 0; t < ms; t += LR_SWITCH_POLL_MS) {
        r = lr_switch_debounce(on, last, n);
    }
    return r;
}

int main(void)
{
    bool last = false;
    int n = 0;
    assert(hold(true, 500, &last, &n) == -1);  // reads over 0.45 s: not yet
    assert(hold(true, 50, &last, &n) == 1);    // the one 0.5 s after the first: on
    for (int i = 0; i < 40; i++) {             // a lever bouncing every 50 ms for 2 s: never settles
        assert(hold(i & 1, 50, &last, &n) == -1);
    }
    assert(hold(false, 550, &last, &n) == 0);  // then off for 0.5 s: off
    assert(hold(true, 450, &last, &n) == -1 && hold(false, 50, &last, &n) == -1);  // on for less: still not on
    assert(hold(true, 550, &last, &n) == 1);
    assert(hold(false, 450, &last, &n) == -1 && hold(true, 600, &last, &n) == 1);  // running: off for less, back on
    puts("switch_check ok");
    return 0;
}
