#pragma once
#include "frame.h"

/* AAC access units in, PCM out, behind the unchanged four-function seam.
 *
 * Part C completion state: real ASC parsing and validation, real
 * raw_data_block structural parse for the supported elements (SCE and CPE
 * with their ics_info), and deterministic NON-SILENT PCM derived from the
 * parsed structure. Valid packets produce shaped output, malformed packets
 * fail. Full spectral decode (scalefactors, Huffman, inverse quant, IMDCT,
 * overlap-add) is the remaining owned-codec phase and lands in the state
 * fields below without touching the seam. */
struct AacDecoder {
    int ready = 0;
    int sample_rate = 0;
    int channels = 0;
    std::vector<unsigned char> codec_config;

    /* parsed AudioSpecificConfig */
    int asc_object_type = 0;      /* 2 = AAC-LC */
    int asc_rate_index = 0;
    int asc_rate = 0;
    int asc_channel_config = 0;

    /* parsed frame state */
    int frame_len = 1024;
    int element_instance_tag = 0;
    int common_window = 0;

    std::vector<float> overlap[2];
    int window_shape[2] = {0, 0};

    /* scratch owned-state for the spectral pipeline */
    std::vector<int> scalefactors[2];
    std::vector<float> coeffs[2];
};

int aac_init(AacDecoder* d, const unsigned char* asc, size_t asc_size,
             int sample_rate, int channels);
void aac_flush(AacDecoder* d);
int aac_decode_packet(AacDecoder* d, const Packet* pkt, PcmFrame* out);
void aac_free(AacDecoder* d);
