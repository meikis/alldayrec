#include <haly/nrfy_gpio.h>

// #define SAMPLE_RATE 16000
#define MIC_GAIN 64
#define MIC_HPF_HZ 80              // 4th-order Butterworth high-pass before the codec: the PDM path carries 2-20 Hz at up to -8 dBFS
#define MIC_IRC_PRIORITY 7
#define MIC_BUFFER_SAMPLES 1600    // 100ms
#define AUDIO_BUFFER_SAMPLES 16000 // 1s
#define NETWORK_RING_BUF_SIZE 32   // number of frames * CODEC_OUTPUT_MAX_BYTES
#define MINIMAL_PACKET_SIZE 100    // Less than that doesn't make sence to send anything at all

// PIN definitions
// https://github.com/Seeed-Studio/Adafruit_nRF52_Arduino/blob/5aa3573913449410fd60f76b75673c53855ff2ec/variants/Seeed_XIAO_nRF52840_Sense/variant.cpp#L34
#include <zephyr/devicetree.h>
#define LR_USER DT_PATH(zephyr_user)
#if DT_NODE_HAS_PROP(LR_USER, pdm_clk_gpios)  // an external PDM mic, wired per a board overlay (lr_promicro.overlay)
#define LR_PIN(prop) NRF_GPIO_PIN_MAP(DT_PROP(DT_GPIO_CTLR(LR_USER, prop), port), DT_GPIO_PIN(LR_USER, prop))
#define PDM_DIN_PIN LR_PIN(pdm_din_gpios)
#define PDM_CLK_PIN LR_PIN(pdm_clk_gpios)
#define PDM_PWR_PIN LR_PIN(pdm_pwr_gpios)
#else  // the XIAO nRF52840 Sense's own mic
#define PDM_DIN_PIN NRF_GPIO_PIN_MAP(0, 16)
#define PDM_CLK_PIN NRF_GPIO_PIN_MAP(1, 0)
#define PDM_PWR_PIN NRF_GPIO_PIN_MAP(1, 10)
#endif

// Codecs
#ifdef CONFIG_OMI_CODEC_OPUS
#define CODEC_OPUS 1
#else
#error "Enable CONFIG_OMI_CODEC_OPUS in the project .conf file"
#endif

#if CODEC_OPUS
#define CODEC_PACKAGE_SAMPLES 160
#define CODEC_OUTPUT_MAX_BYTES CODEC_PACKAGE_SAMPLES * 2 // Let's assume that 16bit is enough
#define CODEC_OPUS_APPLICATION OPUS_APPLICATION_RESTRICTED_LOWDELAY
#define CODEC_OPUS_BITRATE 32000
#define CODEC_OPUS_VBR 1 // Or 1
#define CODEC_OPUS_COMPLEXITY 3
#endif
#define CONFIG_OPUS_MODE CONFIG_OPUS_MODE_CELT

// Codec IDs

#ifdef CODEC_OPUS
#define CODEC_ID 20
#endif

// Logs
// #define LOG_DISCARDED