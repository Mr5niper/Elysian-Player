#pragma once
#include "frame.h"

/* H.264 access units in, frames out. Synthetic frames first, per the plan;
 * the decode decision from CONTRACT.md lands inside this seam later. */
struct H264Decoder {
    int ready = 0;
    int width = 0;
    int height = 0;
    std::vector<unsigned char> codec_config;
};

int h264_init(H264Decoder* d, const unsigned char* avcc, size_t avcc_size,
              int width, int height);
void h264_flush(H264Decoder* d);
int h264_decode_packet(H264Decoder* d, const Packet* pkt, VideoFrame* out);
void h264_free(H264Decoder* d);
