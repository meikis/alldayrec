// Host check of src/mic_ring.h: cc -o mic_ring_check test/mic_ring_check.c && ./mic_ring_check
#include <assert.h>
#include <stdio.h>
#include "../src/mic_ring.h"

static struct mic_ring r;
static int16_t in[MIC_RING_SAMPLES], out[MIC_RING_SAMPLES];
static uint32_t mic_no, frame_no;  // buffers the mic gave; the codec's frame numbers (transport_frame_no)

static bool give(void)  // the mic's next buffer, its first sample its number
{
    in[0] = (int16_t) mic_no++;
    return mic_ring_put(&r, in);
}

static uint32_t take(void)  // the codec: one frame, numbered on past what was left out; returns how many were
{
    uint32_t left_out;
    assert(mic_ring_get(&r, out, &left_out));
    frame_no += left_out;
    assert(out[0] == (int16_t) frame_no);  // the frame gets the number of the buffer it came from
    frame_no++;
    return left_out;
}

int main(void)
{
    uint32_t left_out;
    assert(!mic_ring_get(&r, out, &left_out));  // nothing yet
    for (int i = 0; i < 250; i++) {  // the codec keeps up: no gaps
        assert(give());
        assert(take() == 0);
    }
    // the codec gets no CPU for 1.3 s: the ring fills (1 s), the next 30 buffers are left out
    for (int i = 0; i < MIC_RING_SLOTS; i++) {
        assert(give());
    }
    for (int i = 0; i < 30; i++) {
        assert(!give());
    }
    for (int i = 0; i < MIC_RING_SLOTS; i++) {  // the second in the ring comes first, as it was
        assert(take() == 0);
    }
    assert(give() && take() == 30);  // then the gap, where it was: 30 frames numbered past
    assert(frame_no == mic_no);  // the codec's clock is the mic's again
    // the codec gets the CPU back while the mic is still giving: a gap in the middle of the ring
    for (int i = 0; i < MIC_RING_SLOTS; i++) {
        give();
    }
    take(), take();
    give(), give();
    assert(!give() && !give() && !give());  // 3 left out after the 2 just put
    while (r.put != r.got) {
        take();
    }
    assert(frame_no == mic_no - 3);  // the 3 are not yet counted: they come before the next buffer
    assert(give() && take() == 3 && frame_no == mic_no);
    // the counters wrap
    r.made = r.next = r.put = r.got = UINT32_MAX - 1, mic_no = frame_no = UINT32_MAX - 1;
    for (int i = 0; i < 5; i++) {
        assert(give() && take() == 0);
    }
    printf("mic_ring_check ok\n");
    return 0;
}
