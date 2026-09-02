#pragma once
#include "frame.h"

/* AAC access units in, PCM out. The synthetic implementation produces
 * silence of the correct shape so the pipeline, clocking and drain rules
 * become real first; a hand-rolled or platform decoder replaces the
 * internals later without touching player.cpp (the CONTRACT.md decision). */
struct AacDecoder {
    int ready = 0;
    int sample_rate = 0;
    int channels = 0;
    std::vector<unsigned char> codec_config;
};

int aac_init(AacDecoder* d, const unsigned char* asc, size_t asc_size,
             int sample_rate, int channels);
void aac_flush(AacDecoder* d);
int aac_decode_packet(AacDecoder* d, const Packet* pkt, PcmFrame* out);
void aac_free(AacDecoder* d);
