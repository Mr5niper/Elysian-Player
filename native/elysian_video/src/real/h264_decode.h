#pragma once
#include "frame.h"

/* H.264 access units in, frames out, behind the unchanged four-function
 * seam.
 *
 * C.3 status: h264_init genuinely parses the avcC record (SPS/PPS sets,
 * NAL length size), unescapes RBSP, parses the SPS (profile, level,
 * dimensions with frame cropping, frame_num and POC parameters) and the
 * PPS entry, and validates dimensions against the container's story.
 * h264_decode_packet walks the access unit's length-prefixed NAL units
 * for real; pixel reconstruction (slice decode, intra prediction, CAVLC
 * residuals, transform, MC, deblocking) replaces the synthesis next,
 * building on the parsed state below. */
struct H264Sps {
    int profile_idc = 0;
    int level_idc = 0;
    int log2_max_frame_num = 0;
    int poc_type = 0;
    int log2_max_poc_lsb = 0;
    int mb_width = 0;             /* pic_width_in_mbs */
    int mb_height = 0;            /* pic_height_in_map_units, frames only */
    int width = 0;                /* cropped display width */
    int height = 0;
    int frame_mbs_only = 0;
};

struct H264Pps {
    int entropy_cabac = 0;        /* 0 = CAVLC, 1 = CABAC */
    int pic_init_qp = 0;
};

struct H264Decoder {
    int ready = 0;
    int width = 0;
    int height = 0;
    std::vector<unsigned char> codec_config;

    int nal_length_size = 4;      /* from avcC, 1/2/4 */
    H264Sps sps;
    H264Pps pps;
    int have_sps = 0;
    int have_pps = 0;
};

int h264_init(H264Decoder* d, const unsigned char* avcc, size_t avcc_size,
              int width, int height);
void h264_flush(H264Decoder* d);
int h264_decode_packet(H264Decoder* d, const Packet* pkt, VideoFrame* out);
void h264_free(H264Decoder* d);
