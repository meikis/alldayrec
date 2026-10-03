#include "mic.h"

#include <haly/nrfy_gpio.h>
#include <zephyr/device.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>

#include "config.h"
#include "led.h"
#include <zephyr/drivers/clock_control/nrf_clock_control.h>
#include "nrfx_pdm.h"
#include "utils.h"

LOG_MODULE_REGISTER(mic, CONFIG_LOG_DEFAULT_LEVEL);

//
// Port of this code: https://github.com/Seeed-Studio/Seeed_Arduino_Mic/blob/master/src/hardware/nrf52840_adc.cpp
//

static int16_t _buffer_0[MIC_BUFFER_SAMPLES];
static int16_t _buffer_1[MIC_BUFFER_SAMPLES];
static volatile uint8_t _next_buffer_index = 0;
static volatile mix_handler _callback = NULL;

static void pdm_irq_handler(nrfx_pdm_evt_t const *event)
{
    // Ignore error (how to handle?)
    if (event->error) {
        LOG_ERR("PDM error: %d", event->error);
        return;
    }

    // Assign buffer
    if (event->buffer_requested) {
        LOG_DBG("Audio buffer requested");
        if (_next_buffer_index == 0) {
            nrfx_pdm_buffer_set(_buffer_0, MIC_BUFFER_SAMPLES);
            _next_buffer_index = 1;
        } else {
            nrfx_pdm_buffer_set(_buffer_1, MIC_BUFFER_SAMPLES);
            _next_buffer_index = 0;
        }
    }

    // Release buffer
    if (event->buffer_released) {
        LOG_DBG("Audio buffer requested");
        if (_callback) {
            _callback(event->buffer_released);
        }
    }
}

int mic_start()
{

    // Hold the crystal (HFXO) for as long as the mic runs. The PDM clock is divided from HFCLK; a bare HFCLKSTART
    // is undone whenever the BLE stack (MPSL) releases its own request, and HFCLK falls back to the RC oscillator:
    // on battery (no USB holding HFXO) frames came ~1% fast, 36 s an hour off the frame-clock timestamps.
    static struct onoff_client hfxo;
    sys_notify_init_spinwait(&hfxo.notify);
    int err = onoff_request(z_nrf_clock_control_get_onoff(CLOCK_CONTROL_NRF_SUBSYS_HF), &hfxo);
    int res;
    while (err >= 0 && sys_notify_fetch_result(&hfxo.notify, &res) == -EAGAIN) {
        k_msleep(1);
    }
    if (err < 0) {
        LOG_ERR("HFXO request failed: %d", err);
    }

    // Configure PDM
    nrfx_pdm_config_t pdm_config = NRFX_PDM_DEFAULT_CONFIG(PDM_CLK_PIN, PDM_DIN_PIN);
    pdm_config.gain_l = MIC_GAIN;
    pdm_config.gain_r = MIC_GAIN;
    pdm_config.interrupt_priority = MIC_IRC_PRIORITY;
    pdm_config.clock_freq = NRF_PDM_FREQ_1280K;
    pdm_config.mode = NRF_PDM_MODE_MONO;
    pdm_config.edge = NRF_PDM_EDGE_LEFTFALLING;
    pdm_config.ratio = NRF_PDM_RATIO_80X;
    IRQ_DIRECT_CONNECT(PDM_IRQn, 5, nrfx_pdm_irq_handler, 0); // IMPORTANT!
    if (nrfx_pdm_init(&pdm_config, pdm_irq_handler) != NRFX_SUCCESS) {
        LOG_ERR("Audio unable to initialize PDM");
        return -1;
    }

    // Power on Mic
    nrfy_gpio_cfg_output(PDM_PWR_PIN);
    nrfy_gpio_pin_set(PDM_PWR_PIN);

    // Start PDM
    if (nrfx_pdm_start() != NRFX_SUCCESS) {
        LOG_ERR("Audio unable to start PDM");
        return -1;
    }

    LOG_INF("Audio microphone started");
    return 0;
}

void set_mic_callback(mix_handler callback)
{
    _callback = callback;
}

void mic_off()
{
    nrfy_gpio_pin_clear(PDM_PWR_PIN);
}

void mic_on()
{
    nrfy_gpio_pin_set(PDM_PWR_PIN);
}
