#pragma once
#include "frame.h"

struct H264Sps {
    int profile_idc = 0;
    int level_idc = 0;
    int log2_max_frame_num = 0;
    int poc_type = 0;
    int log2_max_poc_lsb = 0;
    int mb_width = 0;
    int mb_height = 0;
    int width = 0;
    int height = 0;
    int frame_mbs_only = 0;
};

struct H264Pps {
    int entropy_cabac = 0;
    int pic_init_qp = 0;
};

struct H264Decoder {
    int ready = 0;
    int width = 0;
    int height = 0;
    std::vector<unsigned char> codec_config;

    int nal_length_size = 4;
    H264Sps sps;
    H264Pps pps;
    int have_sps = 0;
    int have_pps = 0;

    /* parsed slice state */
    int frame_num = 0;
    int poc_lsb = 0;
    int last_slice_type = -1;
    int seen_idr = 0;
};

int h264_init(H264Decoder* d, const unsigned char* avcc, size_t avcc_size,
              int width, int height);
void h264_flush(H264Decoder* d);
int h264_decode_packet(H264Decoder* d, const Packet* pkt, VideoFrame* out);
void h264_free(H264Decoder* d);
