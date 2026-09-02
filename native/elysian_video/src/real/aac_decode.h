#pragma once
#include "frame.h"

/* AAC access units in, PCM out, behind the unchanged four-function seam.
 *
 * C.3 status: aac_init genuinely parses and validates the
 * AudioSpecificConfig (object type, sample-rate index including the
 * 24-bit escape form, channel configuration) and rejects anything but
 * AAC-LC mono/stereo, per the owned-decoder scope. aac_decode_packet
 * validates real preconditions but still synthesizes silence; the
 * spectral pipeline (ICS parse, scalefactors, Huffman, inverse quant,
 * IMDCT, overlap-add) replaces its body next, using the state fields
 * below. */
struct AacDecoder {
    int ready = 0;
    int sample_rate = 0;
    int channels = 0;
    std::vector<unsigned char> codec_config;

    /* parsed AudioSpecificConfig */
    int asc_object_type = 0;      /* 2 = AAC-LC, the only accepted type */
    int asc_rate_index = 0;
    int asc_rate = 0;             /* resolved Hz, escape form included */
    int asc_channel_config = 0;

    /* decode state reserved for the spectral pipeline */
    std::vector<float> overlap[2];    /* per-channel overlap-add history */
    int window_shape[2] = {0, 0};
};

int aac_init(AacDecoder* d, const unsigned char* asc, size_t asc_size,
             int sample_rate, int channels);
void aac_flush(AacDecoder* d);
int aac_decode_packet(AacDecoder* d, const Packet* pkt, PcmFrame* out);
void aac_free(AacDecoder* d);
