// Host check of src/sdhc_spi.c's waits (#105) against a fake SD card on a fake 8 MHz SPI clock (1 us a byte): an R1
// that comes late, a multi-block read with CMD12's busy, a write's busy, and a card that stays busy. Upstream slept
// 10 + 30 ms per read and polled a stuck card forever; this fails on either, and on a sleep inside a short busy. Also the
// CID it keeps and the failed tries it counts for the event log (#155 F2 F3).
//   d=$(mktemp -d) && mkdir -p $d/zephyr/{drivers,logging,sys} && for h in drivers/sdhc drivers/gpio drivers/spi \
//   logging/log sys/byteorder sys/crc; do touch $d/zephyr/$h.h; done && cc -w -I$d -o $d/sdhc_check \
//   test/sdhc_check.c && $d/sdhc_check
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

// --- the Zephyr API sdhc_spi.c uses
#define LOG_MODULE_REGISTER(...)
#define LOG_ERR(...)
#define LOG_DBG(...)
#define BUILD_ASSERT(...)
#define DT_INST_FOREACH_STATUS_OKAY(x)
#define BIT(n) (1u << (n))
struct device { const void *config; void *data; };
struct gpio_dt_spec { const struct device *port; };
struct spi_config { uint32_t frequency; uint32_t operation; };
struct spi_buf { void *buf; size_t len; };
struct spi_buf_set { const struct spi_buf *buffers; size_t count; };
#define SPI_CS_ACTIVE_HIGH BIT(14)
enum sdhc_power { SDHC_POWER_OFF, SDHC_POWER_ON };
struct sdhc_command { uint32_t opcode, arg, response[4], response_type; int retries, timeout_ms; };
struct sdhc_data { uint32_t block_addr, block_size, blocks; void *data; int timeout_ms; };
struct sdhc_io { uint32_t clock; int bus_mode, power_mode, bus_width, signal_voltage; };
struct sdhc_host_props { uint32_t f_min, f_max, power_delay; struct { bool vol_330_support; } host_caps; bool is_spi; };
struct sdhc_driver_api { void *request, *set_io, *get_host_props, *get_card_present, *reset, *card_busy; };
#define SDHC_BUSMODE_PUSHPULL 1
#define SDHC_BUS_WIDTH1BIT 1
#define SD_VOL_3_3_V 0
#define SDMMC_CLOCK_400KHZ 400000
#define SDHC_SPI_RESPONSE_TYPE_MASK 0xF0
enum { SD_SPI_R1PARAMETER_ERR = BIT(6), SD_SPI_R1ADDRESS_ERR = BIT(5), SD_SPI_R1ERASE_SEQ_ERR = BIT(4),
       SD_SPI_R1CMD_CRC_ERR = BIT(3), SD_SPI_R1ILLEGAL_CMD_ERR = BIT(2), SD_SPI_R1ERASE_RESET = BIT(1) };
enum { SD_RSP_TYPE_R1 = 1, SD_SPI_RSP_TYPE_R1 = 1 << 4, SD_SPI_RSP_TYPE_R1b = 2 << 4, SD_SPI_RSP_TYPE_R2 = 3 << 4,
       SD_SPI_RSP_TYPE_R3 = 4 << 4, SD_SPI_RSP_TYPE_R4 = 5 << 4, SD_SPI_RSP_TYPE_R5 = 6 << 4, SD_SPI_RSP_TYPE_R7 = 7 << 4 };
enum { SD_SEND_CSD = 9, SD_SEND_CID = 10, SD_STOP_TRANSMISSION = 12, SD_SEND_STATUS = 13, SD_READ_SINGLE_BLOCK = 17, SD_READ_MULTIPLE_BLOCK = 18,
       SD_WRITE_SINGLE_BLOCK = 24, SD_WRITE_MULTIPLE_BLOCK = 25 };
#define SD_SPI_CMD_SIZE 6
#define SD_SPI_CMD_BODY_SIZE 5
#define SD_SPI_CRC16_SIZE 2
#define SD_SPI_START 0x80
#define SD_SPI_TX 0x40
#define SD_SPI_CMD 0x3F
#define SD_SPI_TOKEN_SINGLE 0xFE
#define SD_SPI_TOKEN_MULTI_WRITE 0xFC
#define SD_SPI_TOKEN_STOP_TRAN 0xFD
#define SD_SPI_RESPONSE_ACCEPTED 0x05
#define SD_SPI_RESPONSE_CRC_ERR 0x0B
#define SD_SPI_RESPONSE_WRITE_ERR 0x0C
static void sys_put_be32(uint32_t v, uint8_t *p) { p[0] = v >> 24, p[1] = v >> 16, p[2] = v >> 8, p[3] = v; }
static void sys_put_be16(uint16_t v, uint8_t *p) { p[0] = v >> 8, p[1] = v; }
static uint16_t sys_get_be16(const uint8_t *p) { return p[0] << 8 | p[1]; }
static uint8_t crc7_be(uint8_t seed, const uint8_t *p, size_t n) { return 0; }  // SPI mode ignores it after CMD8
static uint16_t crc16_itu_t(uint16_t c, const uint8_t *p, size_t n)
{
    while (n--) {
        c = (uint8_t) (c >> 8) | c << 8;
        c ^= *p++;
        c ^= (uint8_t) (c & 0xff) >> 4;
        c ^= c << 12;
        c ^= (c & 0xff) << 5;
    }
    return c;
}

static uint64_t now;      // us
static uint64_t slept;    // ms asked of k_msleep
static void tick(uint64_t us)
{
    now += us;
    if (now > 60000000) {
        printf("FAIL: still waiting after 60 s (a stuck card polled forever)\n");
        exit(1);
    }
}
static void k_msleep(int ms) { slept += ms, tick(ms ? ms * 1000 : 1); }
static int64_t k_uptime_get(void) { return now / 1000; }
static void k_yield(void) {}
static uint32_t k_ticks_to_ms_floor32(uint32_t t) { return t * 1000 / 32768; }
static bool device_is_ready(const struct device *d) { return true; }
static int gpio_pin_set_dt(const struct gpio_dt_spec *g, int v) { return 0; }
static bool gpio_is_ready_dt(const struct gpio_dt_spec *g) { return true; }
static int gpio_pin_configure_dt(const struct gpio_dt_spec *g, int f) { return 0; }
#define GPIO_OUTPUT_INACTIVE 0

// --- the card: its next bytes on MISO, some held for a time (busy 0x00, or 0xFF while it reads), MOSI decoded as
// commands and write data
static struct { uint8_t b; uint32_t us; } out[4096];
static int out_n, out_i;
static uint64_t held;                     // when the head's hold ends (0: not started)
static int ncr = 1;                       // 0xFF bytes before every R1
static uint32_t nac_us, stop_us, prog_us; // read latency, busy after CMD12, busy programming a block
static uint8_t blk[512], got[512];        // what reads return, what the last write sent
static int crc_errs;                       // writes the card answers with a CRC error token first
static uint8_t cmd[6];
static int cmd_n, rx_n = -1;              // rx_n >= 0: taking a written block (token, 512 B, CRC)

static void put(uint8_t b, uint32_t us) { out[out_n].b = b, out[out_n++].us = us; }
static void put_block(int n)  // a data block: the first n bytes of blk
{
    uint16_t crc = crc16_itu_t(0, blk, n);
    put(0xFF, nac_us), put(SD_SPI_TOKEN_SINGLE, 0);
    for (int i = 0; i < n; i++) {
        put(blk[i], 0);
    }
    put(crc >> 8, 0), put(crc, 0);
}

static uint8_t clock_byte(uint8_t mosi)
{
    uint8_t miso = 0xFF;
    if (out_i < out_n) {
        miso = out[out_i].b;
        if (!out[out_i].us) {
            out_i++;
        } else if (!held) {
            held = now + out[out_i].us;
        } else if (now >= held) {
            held = 0, out_i++, miso = out_i < out_n ? out[out_i++].b : 0xFF;
        }
    }
    tick(1);
    if (rx_n >= 0) {  // a write's data: token, 512 bytes, CRC
        if (rx_n == 0 && mosi == SD_SPI_TOKEN_STOP_TRAN) {
            out_n = out_i = 0, put(0xFF, 0), put(0, prog_us), rx_n = -1;
        } else if (rx_n > 0 || mosi == SD_SPI_TOKEN_SINGLE || mosi == SD_SPI_TOKEN_MULTI_WRITE) {
            if (rx_n >= 1 && rx_n <= 512) {
                got[rx_n - 1] = mosi;
            }
            if (++rx_n == 515) {  // accepted, then busy programming
                out_n = out_i = 0, put(crc_errs ? SD_SPI_RESPONSE_CRC_ERR : SD_SPI_RESPONSE_ACCEPTED, 0), put(0, prog_us);
                crc_errs -= crc_errs > 0;
                rx_n = cmd[0] == (0x40 | SD_WRITE_MULTIPLE_BLOCK) ? 0 : -1;
            }
        }
        return miso;
    }
    if (cmd_n || (mosi & 0xC0) == 0x40) {
        cmd[cmd_n++] = mosi;
        if (cmd_n == 6) {
            int c = cmd[0] & 0x3F;
            cmd_n = 0, out_n = out_i = 0, held = 0;
            if (c == SD_STOP_TRANSMISSION) {
                put(0x5A, 0);  // a stuff byte, then R1 and busy while the card stops
            }
            for (int i = 0; i < ncr; i++) {
                put(0xFF, 0);
            }
            put(0, 0);  // R1: no error
            if (c == SD_STOP_TRANSMISSION) {
                put(0, stop_us);
            } else if (c == SD_READ_SINGLE_BLOCK) {
                put_block(512);
            } else if (c == SD_SEND_CID || c == SD_SEND_CSD) {
                put_block(16);
            } else if (c == SD_READ_MULTIPLE_BLOCK) {
                for (int i = 0; i < 4; i++) {
                    put_block(512);
                }
            } else if (c == SD_WRITE_SINGLE_BLOCK || c == SD_WRITE_MULTIPLE_BLOCK) {
                rx_n = 0;
            } else if (c == SD_SEND_STATUS) {
                put(0, 0);
            }
        }
    }
    return miso;
}

static int spi_transceive(const struct device *d, const struct spi_config *cfg, const struct spi_buf_set *tx,
                          const struct spi_buf_set *rx)
{
    size_t tn = 0, rn = 0;
    for (size_t i = 0; tx && i < tx->count; i++) {
        tn += tx->buffers[i].len;
    }
    for (size_t i = 0; rx && i < rx->count; i++) {
        rn += rx->buffers[i].len;
    }
    for (size_t k = 0; k < (tn > rn ? tn : rn); k++) {
        uint8_t mosi = 0xFF;  // the overrun character
        for (size_t i = 0, o = k; tx && i < tx->count; o -= tx->buffers[i++].len) {
            if (o < tx->buffers[i].len) {
                mosi = ((const uint8_t *) tx->buffers[i].buf)[o];
                break;
            }
        }
        uint8_t miso = clock_byte(mosi);
        for (size_t i = 0, o = k; rx && i < rx->count; o -= rx->buffers[i++].len) {
            if (o < rx->buffers[i].len) {
                ((uint8_t *) rx->buffers[i].buf)[o] = miso;
                break;
            }
        }
    }
    return 0;
}
static int spi_write(const struct device *d, const struct spi_config *c, const struct spi_buf_set *tx)
{
    return spi_transceive(d, c, tx, NULL);
}
static int spi_release(const struct device *d, const struct spi_config *c) { return 0; }

#include "../src/sdhc_spi.c"
uint8_t lr_sd_cid[16], lr_sd_csd[16];  // sdcard.c's
uint32_t lr_sd_err[LR_SD_ERR_N];

static struct sdhc_spi_config cfg = {.spi_max_freq = 8000000};
static struct sdhc_spi_data data;
static const struct device dev = {&cfg, &data};

static int request(int op, uint32_t blocks, uint8_t *buf, uint64_t *us, uint64_t *ms)
{
    struct sdhc_command c = {.opcode = op, .response_type = SD_RSP_TYPE_R1 | SD_SPI_RSP_TYPE_R1, .retries = 3,
                             .timeout_ms = 200};
    struct sdhc_data d = {.block_size = op == SD_SEND_CID || op == SD_SEND_CSD ? 16 : 512, .blocks = blocks,
                          .data = buf, .timeout_ms = 10000};
    uint64_t t = now, s = slept;
    data.spi_cfg = &data.cfg_a;
    int r = sdhc_spi_request(&dev, &c, &d);
    *us = now - t, *ms = slept - s;
    return r;
}

int main(void)
{
    uint8_t buf[2 * 512];
    uint64_t us, ms;
    for (int i = 0; i < 512; i++) {
        blk[i] = i * 7 + 3;
    }

    // a 2-block read: R1 7 bytes late, 300 us to the first data, 2 ms of busy after CMD12; upstream slept 10 + 30 ms
    ncr = 7, nac_us = 300, stop_us = 2000;
    assert(request(SD_READ_MULTIPLE_BLOCK, 2, buf, &us, &ms) == 0);
    assert(!memcmp(buf, blk, 512) && !memcmp(buf + 512, blk, 512));
    printf("read 2 blocks: %llu us, slept %llu ms\n", (unsigned long long) us, (unsigned long long) ms);
    assert(ms == 0 && us < 4000);

    // one block written, 3 ms programming
    ncr = 1, prog_us = 3000;
    memcpy(buf, blk, 512);
    buf[0] ^= 0xFF;
    assert(request(SD_WRITE_SINGLE_BLOCK, 1, buf, &us, &ms) == 0);
    assert(!memcmp(got, buf, 512));
    printf("write 1 block: %llu us, slept %llu ms\n", (unsigned long long) us, (unsigned long long) ms);
    assert(ms == 0 && us < 4000);  // polled through the 3 ms: a sleep there costs more than it is (codec, #105)

    // the CID as init reads it in SPI mode, kept for CARD_ID (#155 F2); nothing failed so far
    assert(request(SD_SEND_CID, 1, buf, &us, &ms) == 0 && !memcmp(lr_sd_cid, blk, 16));
    static const uint32_t none[LR_SD_ERR_N];
    assert(!memcmp(lr_sd_err, none, sizeof(none)));

    // a write the card takes with a CRC error token first: tried again (#155 F3)
    prog_us = 0, crc_errs = 1;
    assert(request(SD_WRITE_SINGLE_BLOCK, 1, buf, &us, &ms) == 0);
    assert(lr_sd_err[LR_SD_CRC] == 1 && lr_sd_err[LR_SD_RETRIED] == 1 && lr_sd_err[LR_SD_TIMEOUT] == 0);
    memset(lr_sd_err, 0, sizeof(lr_sd_err));

    // a card that stays busy after a write: an error after the data timeout (10 s) per try, 3 tries as card_write()
    // makes them (CONFIG_SD_DATA_RETRIES), not a poll forever
    prog_us = 100000000;
    int r = request(SD_WRITE_SINGLE_BLOCK, 1, buf, &us, &ms);
    printf("stuck card: %d after %llu ms\n", r, (unsigned long long) us / 1000);
    assert(r == -ETIMEDOUT && us >= 30000000 && us < 31000000);
    assert(lr_sd_err[LR_SD_TIMEOUT] == 3 && lr_sd_err[LR_SD_BUSY] == 3 && lr_sd_err[LR_SD_RETRIED] == 2);
    assert(lr_sd_err[LR_SD_CRC] == 0 && lr_sd_err[LR_SD_OTHER] == 0);
    memset(lr_sd_err, 0, sizeof(lr_sd_err));

    // a card that answers a read and never sends the data: a timeout after SD_SPI_SKIP_MS per try, 3 tries; upstream
    // polled 1e6 bytes per try (1 s here, ~100 s on the board) and called it -EIO
    prog_us = 0, nac_us = 100000000;
    r = request(SD_READ_SINGLE_BLOCK, 1, buf, &us, &ms);
    printf("read never answered: %d after %llu ms\n", r, (unsigned long long) us / 1000);
    assert(r == -ETIMEDOUT && us >= 3 * SD_SPI_SKIP_MS * 1000 && us < 3 * SD_SPI_SKIP_MS * 1000 + 50000);
    assert(lr_sd_err[LR_SD_TIMEOUT] == 3 && lr_sd_err[LR_SD_RETRIED] == 2);

    printf("sdhc_check: ok\n");
    return 0;
}
