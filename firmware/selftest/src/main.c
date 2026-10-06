/* Board self-test for nRF52840 Pro Micro boards (nice!nano and its clones): flash it onto every new board before our
 * firmware. Commands come over the USB serial port, one per line; each answers with one result line "R {json}" whose
 * "ok" is the board's verdict: info gpio adc rx led clk N dcdc all dfu. selftest.py drives it and adds the checks that
 * need the host (the 32 MHz crystal against the Mac's clock, the board's advertising as the Mac hears it). README.md
 * says what each check proves and its pass mark. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <zephyr/kernel.h>
#include <zephyr/init.h>
#include <zephyr/device.h>
#include <zephyr/drivers/uart.h>
#include <zephyr/drivers/adc.h>
#include <zephyr/drivers/clock_control.h>
#include <zephyr/drivers/clock_control/nrf_clock_control.h>
#include <zephyr/bluetooth/bluetooth.h>
#include <hal/nrf_gpio.h>
#include <hal/nrf_saadc.h>

#define CYC_PER_US 64  /* the CPU cycle counter: 64 MHz, from the 32 MHz crystal once it runs */
static inline uint32_t cyc(void) { return DWT->CYCCNT; }

/* Clocks, measured before the kernel starts its clocks (it runs on the RC oscillators, prj.conf) */
static struct {
	uint32_t resetreas;
	bool hfxo_ok, lfxo_ok;
	uint32_t hfxo_us, lfxo_ms;
	int32_t lfxo_ppb;  /* the 32.768 kHz crystal against the 32 MHz one */
} early;

static bool wait(const volatile uint32_t *reg, uint32_t mask, bool set, uint32_t us)
{
	for (uint32_t t = cyc(); cyc() - t < us * CYC_PER_US;) {
		if (!(*reg & mask) == !set) {
			return true;
		}
	}
	return false;
}

static int early_clocks(void)
{
	early.resetreas = NRF_POWER->RESETREAS;
	NRF_POWER->RESETREAS = 0xFFFFFFFF;
	CoreDebug->DEMCR |= CoreDebug_DEMCR_TRCENA_Msk;
	DWT->CYCCNT = 0;
	DWT->CTRL |= DWT_CTRL_CYCCNTENA_Msk;

	/* 32 MHz crystal: (re)start it and time it until stable; the datasheet's figure is well under 1 ms */
	NRF_CLOCK->TASKS_HFCLKSTOP = 1;
	wait(&NRF_CLOCK->HFCLKSTAT, CLOCK_HFCLKSTAT_SRC_Msk, false, 10000);
	NRF_CLOCK->EVENTS_HFCLKSTARTED = 0;
	uint32_t t0 = cyc();
	NRF_CLOCK->TASKS_HFCLKSTART = 1;
	early.hfxo_ok = wait(&NRF_CLOCK->EVENTS_HFCLKSTARTED, 1, true, 100000);
	early.hfxo_us = (cyc() - t0) / CYC_PER_US;

	/* 32.768 kHz crystal: the source can change only while the clock is stopped; typically stable in 0.25 s */
	NRF_CLOCK->TASKS_LFCLKSTOP = 1;
	wait(&NRF_CLOCK->LFCLKSTAT, CLOCK_LFCLKSTAT_STATE_Msk, false, 10000);
	NRF_CLOCK->LFCLKSRC = CLOCK_LFCLKSRC_SRC_Xtal << CLOCK_LFCLKSRC_SRC_Pos;
	NRF_CLOCK->EVENTS_LFCLKSTARTED = 0;
	t0 = cyc();
	NRF_CLOCK->TASKS_LFCLKSTART = 1;
	early.lfxo_ok = wait(&NRF_CLOCK->EVENTS_LFCLKSTARTED, 1, true, 2000000);
	early.lfxo_ms = (cyc() - t0) / CYC_PER_US / 1000;
	if (early.lfxo_ok && early.hfxo_ok) {
		/* 65536 RTC ticks (2 s) on the crystal take exactly 128e6 CPU cycles when both crystals are exact; interrupts
		 * are not running yet, so the edges are timed to a cycle */
		NRF_RTC2->PRESCALER = 0;
		NRF_RTC2->TASKS_CLEAR = 1;
		NRF_RTC2->TASKS_START = 1;
		uint32_t c = NRF_RTC2->COUNTER;
		while (NRF_RTC2->COUNTER == c) {
		}
		uint32_t a = cyc(), c0 = NRF_RTC2->COUNTER;
		while (NRF_RTC2->COUNTER - c0 < 65536) {
		}
		uint32_t dt = cyc() - a;
		NRF_RTC2->TASKS_STOP = 1;
		early.lfxo_ppb = (int32_t)((128000000LL - dt) * 1000000000LL / dt);
	}

	/* Hand both clocks back stopped; the kernel starts them its own way */
	NRF_CLOCK->TASKS_LFCLKSTOP = 1;
	wait(&NRF_CLOCK->LFCLKSTAT, CLOCK_LFCLKSTAT_STATE_Msk, false, 10000);
	NRF_CLOCK->LFCLKSRC = CLOCK_LFCLKSRC_SRC_RC << CLOCK_LFCLKSRC_SRC_Pos;
	NRF_CLOCK->EVENTS_LFCLKSTARTED = 0;
	NRF_CLOCK->TASKS_HFCLKSTOP = 1;
	NRF_CLOCK->EVENTS_HFCLKSTARTED = 0;
	return 0;
}
SYS_INIT(early_clocks, PRE_KERNEL_1, 0);

/* The Pro Micro footprint's GPIOs (USB up; left row, right row, the three pads at the bottom edge). Not here: RST
 * (P0.18), the LED (P0.15) and the VCC switch (P0.13), which have parts on them */
#define P(port, n) NRF_GPIO_PIN_MAP(port, n)
static const uint8_t pins[] = {
	P(0, 6), P(0, 8), P(0, 17), P(0, 20), P(0, 22), P(0, 24), P(1, 0), P(0, 11), P(1, 4), P(1, 6),
	P(0, 31), P(0, 29), P(0, 2), P(1, 15), P(1, 13), P(1, 11), P(0, 10), P(0, 9),
	P(1, 1), P(1, 2), P(1, 7),
};
#define LED_PIN P(0, 15)

static char *pin_name(uint8_t p, char *buf)
{
	sprintf(buf, "P%u.%02u", p >> 5, p & 31);
	return buf;
}

static void pins_input(nrf_gpio_pin_pull_t pull)
{
	ARRAY_FOR_EACH(pins, i) {
		nrf_gpio_cfg_input(pins[i], pull);
	}
}

static bool nfc_pin(uint8_t p)
{
	/* P0.09/P0.10 are NFC antenna pins unless UICR NFCPINS says GPIO */
	return (p == P(0, 9) || p == P(0, 10)) && (NRF_UICR->NFCPINS & UICR_NFCPINS_PROTECT_Msk);
}

/* Each pin alone: pulled up it must read 1, pulled down 0 (else it is tied to GND or VDD). Then each pin driven low
 * with all others pulled up, and driven high with all others pulled down: another pin that follows it is shorted to
 * it (a solder bridge). A pin with no contact to its pad passes: that needs a wire on the pad. */
static void cmd_gpio(void)
{
	static char out[700];
	char a[8], b[8];
	int n = 0, bad = 0;

	n += snprintf(out + n, sizeof(out) - n, "\"stuck\":[");
	ARRAY_FOR_EACH(pins, i) {
		uint8_t p = pins[i];

		if (nfc_pin(p)) {
			continue;
		}
		nrf_gpio_cfg_input(p, NRF_GPIO_PIN_PULLUP);
		k_busy_wait(100);
		bool up = nrf_gpio_pin_read(p);
		nrf_gpio_cfg_input(p, NRF_GPIO_PIN_PULLDOWN);
		k_busy_wait(100);
		bool down = nrf_gpio_pin_read(p);
		nrf_gpio_cfg_default(p);
		if (!up || down) {
			n += snprintf(out + n, sizeof(out) - n, "%s\"%s %s\"", bad++ ? "," : "", pin_name(p, a),
				      !up ? "low" : "high");
		}
	}
	int shorts = 0;
	n += snprintf(out + n, sizeof(out) - n, "],\"shorts\":[");
	for (int level = 0; level < 2; level++) {
		ARRAY_FOR_EACH(pins, i) {
			if (nfc_pin(pins[i])) {
				continue;
			}
			pins_input(level ? NRF_GPIO_PIN_PULLDOWN : NRF_GPIO_PIN_PULLUP);
			nrf_gpio_cfg_output(pins[i]);
			nrf_gpio_pin_write(pins[i], level);
			k_busy_wait(100);
			ARRAY_FOR_EACH(pins, j) {
				if (j > i && !nfc_pin(pins[j]) && nrf_gpio_pin_read(pins[j]) == (uint32_t)level &&
				    n < (int)sizeof(out) - 40) {
					n += snprintf(out + n, sizeof(out) - n, "%s\"%s-%s\"", shorts++ ? "," : "",
						      pin_name(pins[i], a), pin_name(pins[j], b));
				}
			}
			nrf_gpio_cfg_default(pins[i]);
		}
	}
	ARRAY_FOR_EACH(pins, i) {
		nrf_gpio_cfg_default(pins[i]);
	}
	printk("R {\"test\":\"gpio\",\"ok\":%d,\"pins\":%u,\"nfc_as_gpio\":%d,%s]}\n", !bad && !shorts,
	       (unsigned)ARRAY_SIZE(pins), !nfc_pin(P(0, 9)), out);
}

static const struct device *const adc = DEVICE_DT_GET(DT_NODELABEL(adc));

/* One SAADC reading in mV: gain 1/6 on the 0.6 V reference reads 0 to 3.6 V */
static int adc_mv(uint8_t input)
{
	struct adc_channel_cfg ch = {
		.gain = ADC_GAIN_1_6,
		.reference = ADC_REF_INTERNAL,
		.acquisition_time = ADC_ACQ_TIME(ADC_ACQ_TIME_MICROSECONDS, 40),
		.input_positive = input,
	};
	int16_t raw;
	struct adc_sequence seq = {
		.channels = BIT(0),
		.buffer = &raw,
		.buffer_size = sizeof(raw),
		.resolution = 12,
		.oversampling = 4,
		.calibrate = true,
	};

	if (adc_channel_setup(adc, &ch) || adc_read(adc, &seq)) {
		return -1;
	}
	int32_t mv = MAX(raw, 0);
	adc_raw_to_millivolts(600, ADC_GAIN_1_6, 12, &mv);
	return mv;
}

/* What UICR REGOUT0 sets VDD to (the chip runs from VDDH: USB or battery), in mV; erased = 1.8 V */
static int vdd_expected(void)
{
	static const int mv[8] = {1800, 2100, 2400, 2700, 3000, 3300, 1800, 1800};

	return mv[NRF_UICR->REGOUT0 & UICR_REGOUT0_VOUT_Msk];
}

static bool vdd_ok(int mv)
{
	int e = vdd_expected();

	return mv > e * 95 / 100 && mv < e * 105 / 100;
}

/* VDD from the chip's regulator; VDDH, where USB (through the board's diode) and the battery come in: on USB it reads
 * the USB side; P0.04 (AIN2), the nice!nano v2's battery divider, which a clone may not have */
static void cmd_adc(void)
{
	int vdd = adc_mv(NRF_SAADC_INPUT_VDD);
	int vddh = adc_mv(NRF_SAADC_INPUT_VDDHDIV5) * 5;
	int ain2 = adc_mv(NRF_SAADC_INPUT_AIN2);

	printk("R {\"test\":\"adc\",\"ok\":%d,\"vdd_mv\":%d,\"vdd_expected_mv\":%d,\"vddh_mv\":%d,\"p0_04_mv\":%d}\n",
	       vdd_ok(vdd) && vddh > 4300 && vddh < 5500, vdd, vdd_expected(), vddh, ain2);
}

static bt_addr_le_t seen[64];
static int nseen;
static int8_t best_rssi;

static void scan_cb(const bt_addr_le_t *addr, int8_t rssi, uint8_t type, struct net_buf_simple *ad)
{
	best_rssi = MAX(best_rssi, rssi);
	for (int i = 0; i < nseen; i++) {
		if (bt_addr_le_eq(&seen[i], addr)) {
			return;
		}
	}
	if (nseen < (int)ARRAY_SIZE(seen)) {
		bt_addr_le_copy(&seen[nseen++], addr);
	}
}

/* The radio's receive side: 5 s of passive scanning must hear someone's advertising (phones and laptops nearby) */
static void cmd_rx(void)
{
	nseen = 0;
	best_rssi = -127;
	int err = bt_le_scan_start(BT_LE_SCAN_PASSIVE, scan_cb);

	k_sleep(K_SECONDS(5));
	bt_le_scan_stop();
	printk("R {\"test\":\"rx\",\"ok\":%d,\"err\":%d,\"devices\":%d,\"best_rssi\":%d}\n",
	       !err && nseen > 0 && best_rssi >= -90, err, nseen, best_rssi);
}

/* The blue LED, by eye: five blinks */
static void cmd_led(void)
{
	nrf_gpio_cfg_output(LED_PIN);
	for (int i = 0; i < 5; i++) {
		nrf_gpio_pin_set(LED_PIN);
		k_msleep(300);
		nrf_gpio_pin_clear(LED_PIN);
		k_msleep(300);
	}
	nrf_gpio_cfg_default(LED_PIN);
	printk("R {\"test\":\"led\",\"blinks\":5}\n");
}

/* The 32 MHz crystal's own count (TIMER4 at 16 MHz) for secs seconds, 20 lines "T i ticks" a second; the host fits
 * it against its clock */
static void cmd_clk(int secs)
{
	struct onoff_manager *hf = z_nrf_clock_control_get_onoff(CLOCK_CONTROL_NRF_SUBSYS_HF);
	struct onoff_client cli;
	int res = -1;

	sys_notify_init_spinwait(&cli.notify);
	onoff_request(hf, &cli);
	while (sys_notify_fetch_result(&cli.notify, &res) == -EAGAIN) {
	}
	NRF_TIMER4->MODE = TIMER_MODE_MODE_Timer;
	NRF_TIMER4->BITMODE = TIMER_BITMODE_BITMODE_32Bit;
	NRF_TIMER4->PRESCALER = 0;
	NRF_TIMER4->TASKS_CLEAR = 1;
	NRF_TIMER4->TASKS_START = 1;
	uint64_t hi = 0;
	uint32_t last = 0;
	for (int i = 0; i < secs * 20; i++) {
		NRF_TIMER4->TASKS_CAPTURE[0] = 1;
		uint32_t now = NRF_TIMER4->CC[0];

		if (now < last) {
			hi += 1ULL << 32;
		}
		last = now;
		printk("T %d %llu\n", i, hi | now);
		k_msleep(50);
	}
	NRF_TIMER4->TASKS_STOP = 1;
	onoff_release(hf);
	printk("R {\"test\":\"clk\",\"hz\":16000000,\"lines\":%d}\n", secs * 20);
}

/* The DCDC converters (each needs its inductor; without one the chip browns out and resets, which the host sees as
 * the port going away after an "S" line): REG1 (DCDCEN), then REG0 (DCDCEN0, the high-voltage stage), VDD after each */
static void cmd_dcdc(void)
{
	int v[3];

	v[0] = adc_mv(NRF_SAADC_INPUT_VDD);
	printk("S dcdc reg1\n");
	k_msleep(200);
	NRF_POWER->DCDCEN = 1;
	k_msleep(300);
	v[1] = adc_mv(NRF_SAADC_INPUT_VDD);
	printk("S dcdc reg0\n");
	k_msleep(200);
	NRF_POWER->DCDCEN0 = 1;
	k_msleep(300);
	v[2] = adc_mv(NRF_SAADC_INPUT_VDD);
	NRF_POWER->DCDCEN0 = 0;
	NRF_POWER->DCDCEN = 0;
	printk("R {\"test\":\"dcdc\",\"ok\":%d,\"vdd_ldo_mv\":%d,\"vdd_reg1_mv\":%d,\"vdd_reg0_mv\":%d}\n",
	       vdd_ok(v[0]) && vdd_ok(v[1]) && vdd_ok(v[2]), v[0], v[1], v[2]);
}

static void cmd_info(void)
{
	uint32_t var = NRF_FICR->INFO.VARIANT;
	/* the crystals' accuracy needs the host's clock (selftest.py); here only that both start */
	bool lf_ok = early.lfxo_ok && early.lfxo_ms < 1000;

	printk("R {\"test\":\"info\",\"ok\":%d,\"id\":\"%08X%08X\",\"part\":\"%X\",\"variant\":\"%c%c%c%c\","
	       "\"package\":\"%X\",\"ram_kb\":%u,\"flash_kb\":%u,\"resetreas\":\"0x%X\",\"regout0\":\"0x%X\","
	       "\"pselreset\":\"0x%X\",\"nfcpins\":\"0x%X\",\"approtect\":\"0x%X\",\"bootloader\":\"0x%X\","
	       "\"hfxo_ok\":%d,\"hfxo_us\":%u,\"lfxo_ok\":%d,\"lfxo_ms\":%u,\"lfxo_ppb_vs_hfxo\":%d}\n",
	       early.hfxo_ok && early.hfxo_us < 5000 && lf_ok, NRF_FICR->DEVICEID[1], NRF_FICR->DEVICEID[0],
	       NRF_FICR->INFO.PART, (int)(var >> 24), (int)(var >> 16) & 0xFF, (int)(var >> 8) & 0xFF, (int)var & 0xFF, NRF_FICR->INFO.PACKAGE,
	       NRF_FICR->INFO.RAM, NRF_FICR->INFO.FLASH, early.resetreas, NRF_UICR->REGOUT0, NRF_UICR->PSELRESET[0],
	       NRF_UICR->NFCPINS, NRF_UICR->APPROTECT, NRF_UICR->NRFFW[0], early.hfxo_ok, early.hfxo_us,
	       early.lfxo_ok, early.lfxo_ms, early.lfxo_ppb);
}

static void run(char *line)
{
	if (!strcmp(line, "info")) {
		cmd_info();
	} else if (!strcmp(line, "gpio")) {
		cmd_gpio();
	} else if (!strcmp(line, "adc")) {
		cmd_adc();
	} else if (!strcmp(line, "rx")) {
		cmd_rx();
	} else if (!strcmp(line, "led")) {
		cmd_led();
	} else if (!strncmp(line, "clk", 3)) {
		cmd_clk(CLAMP(atoi(line + 3), 1, 600));
	} else if (!strcmp(line, "dcdc")) {
		cmd_dcdc();
	} else if (!strcmp(line, "all")) {  /* everything the board judges alone, for a plain serial terminal */
		cmd_info();
		cmd_gpio();
		cmd_adc();
		cmd_rx();
		cmd_dcdc();
	} else if (!strcmp(line, "dfu")) {  /* into the UF2 bootloader: Adafruit's GPREGRET magic */
		printk("R {\"test\":\"dfu\"}\n");
		k_msleep(200);
		NRF_POWER->GPREGRET = 0x57;
		NVIC_SystemReset();
	} else {
		printk("R {\"test\":\"?\",\"commands\":\"info gpio adc rx led clk N dcdc all dfu\"}\n");
	}
}

int main(void)
{
	const struct device *con = DEVICE_DT_GET(DT_CHOSEN(zephyr_console));
	static char name[24];

	/* Non-connectable advertising for the host to hear, named after the chip like the USB serial number */
	snprintf(name, sizeof(name), "LRT-%08X%08X", NRF_FICR->DEVICEID[1], NRF_FICR->DEVICEID[0]);
	const struct bt_data ad[] = {
		BT_DATA_BYTES(BT_DATA_FLAGS, BT_LE_AD_NO_BREDR),
		BT_DATA(BT_DATA_NAME_COMPLETE, name, strlen(name)),
	};
	int err = bt_enable(NULL);

	if (!err) {
		err = bt_le_adv_start(BT_LE_ADV_NCONN, ad, ARRAY_SIZE(ad), NULL, 0);
	}
	if (err) {
		printk("R {\"test\":\"bt\",\"ok\":0,\"err\":%d}\n", err);
	}

	char line[32];
	size_t n = 0;

	for (;;) {
		unsigned char c;

		if (uart_poll_in(con, &c)) {
			k_msleep(10);
		} else if (c == '\r' || c == '\n') {
			line[n] = 0;
			if (n) {
				run(line);
			}
			n = 0;
		} else if (n < sizeof(line) - 1) {
			line[n++] = c;
		}
	}
}
