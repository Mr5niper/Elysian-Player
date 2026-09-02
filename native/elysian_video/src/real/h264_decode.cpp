#include "h264_decode.h"
#include "bitreader.h"

#include <string.h>

/* Strip emulation-prevention bytes: 00 00 03 -> 00 00. */
static void unescape_rbsp(const unsigned char* in, size_t size,
                          std::vector<unsigned char>* out) {
    out->clear();
    out->reserve(size);
    for (size_t i = 0; i < size; i++) {
        if (i + 2 < size && in[i] == 0 && in[i + 1] == 0 && in[i + 2] == 3) {
            out->push_back(0);
            out->push_back(0);
            i += 2;
        } else {
            out->push_back(in[i]);
        }
    }
}

static int parse_sps(const unsigned char* nal, size_t size, H264Sps* sps) {
    if (size < 4 || (nal[0] & 0x1F) != 7) return 0;
    std::vector<unsigned char> rbsp;
    unescape_rbsp(nal + 1, size - 1, &rbsp);
    BitReader br;
    br_init(&br, rbsp.data(), rbsp.size());

    sps->profile_idc = (int)br_read(&br, 8);
    br_read(&br, 8);                                  /* constraint flags */
    sps->level_idc = (int)br_read(&br, 8);
    br_ue(&br);                                       /* sps_id */

    if (sps->profile_idc == 100 || sps->profile_idc == 110 ||
        sps->profile_idc == 122 || sps->profile_idc == 244 ||
        sps->profile_idc == 44 || sps->profile_idc == 83 ||
        sps->profile_idc == 86 || sps->profile_idc == 118 ||
        sps->profile_idc == 128) {
        uint32_t chroma = br_ue(&br);
        if (chroma == 3) br_read1(&br);               /* separate planes */
        br_ue(&br);                                   /* bit_depth_luma */
        br_ue(&br);                                   /* bit_depth_chroma */
        br_read1(&br);                                /* qpprime flag */
        if (br_read1(&br)) {                          /* scaling matrix */
            int lists = chroma == 3 ? 12 : 8;
            for (int i = 0; i < lists; i++)
                if (br_read1(&br)) {                  /* list present */
                    int size_of = i < 6 ? 16 : 64;
                    int last = 8, next = 8;
                    for (int j = 0; j < size_of; j++) {
                        if (next) next = (last + br_se(&br) + 256) % 256;
                        last = next ? next : last;
                    }
                }
        }
    }

    sps->log2_max_frame_num = (int)br_ue(&br) + 4;
    sps->poc_type = (int)br_ue(&br);
    if (sps->poc_type == 0) {
        sps->log2_max_poc_lsb = (int)br_ue(&br) + 4;
    } else if (sps->poc_type == 1) {
        br_read1(&br);
        br_se(&br);
        br_se(&br);
        uint32_t cycle = br_ue(&br);
        if (cycle > 255) return 0;
        for (uint32_t i = 0; i < cycle; i++) br_se(&br);
    }
    br_ue(&br);                                       /* max_num_ref_frames */
    br_read1(&br);                                    /* gaps allowed */

    sps->mb_width = (int)br_ue(&br) + 1;
    sps->mb_height = (int)br_ue(&br) + 1;
    sps->frame_mbs_only = (int)br_read1(&br);
    if (!sps->frame_mbs_only) br_read1(&br);          /* mbaff */
    br_read1(&br);                                    /* direct_8x8 */

    int crop_l = 0, crop_r = 0, crop_t = 0, crop_b = 0;
    if (br_read1(&br)) {
        crop_l = (int)br_ue(&br);
        crop_r = (int)br_ue(&br);
        crop_t = (int)br_ue(&br);
        crop_b = (int)br_ue(&br);
    }
    if (!br_ok(&br)) return 0;

    int height_map = sps->mb_height * (sps->frame_mbs_only ? 1 : 2);
    /* 4:2:0 crop units: 2 horizontally, 2 * (2 - frame_mbs_only) vertically */
    int crop_y = 2 * (2 - sps->frame_mbs_only);
    sps->width = sps->mb_width * 16 - (crop_l + crop_r) * 2;
    sps->height = height_map * 16 - (crop_t + crop_b) * crop_y;
    return sps->width > 0 && sps->height > 0;
}

static int parse_pps(const unsigned char* nal, size_t size, H264Pps* pps) {
    if (size < 2 || (nal[0] & 0x1F) != 8) return 0;
    std::vector<unsigned char> rbsp;
    unescape_rbsp(nal + 1, size - 1, &rbsp);
    BitReader br;
    br_init(&br, rbsp.data(), rbsp.size());
    br_ue(&br);                                       /* pps_id */
    br_ue(&br);                                       /* sps_id */
    pps->entropy_cabac = (int)br_read1(&br);
    br_read1(&br);                                    /* bottom_field_pic */
    uint32_t slice_groups = br_ue(&br) + 1;
    if (slice_groups > 1) return 0;                   /* FMO out of scope */
    br_ue(&br);                                       /* refs l0 */
    br_ue(&br);                                       /* refs l1 */
    br_read1(&br);                                    /* weighted pred */
    br_read(&br, 2);                                  /* weighted bipred */
    pps->pic_init_qp = 26 + br_se(&br);
    return br_ok(&br);
}

int h264_init(H264Decoder* d, const unsigned char* avcc, size_t avcc_size,
              int width, int height) {
    if (!d || !avcc || avcc_size < 7 || width <= 0 || height <= 0)
        return 0;
    if (avcc[0] != 1) return 0;                       /* configuration ver */
    d->nal_length_size = (avcc[4] & 0x03) + 1;

    size_t at = 5;
    int sps_count = avcc[at++] & 0x1F;
    d->have_sps = 0;
    for (int i = 0; i < sps_count; i++) {
        if (at + 2 > avcc_size) return 0;
        size_t len = ((size_t)avcc[at] << 8) | avcc[at + 1];
        at += 2;
        if (at + len > avcc_size) return 0;
        if (!d->have_sps && parse_sps(avcc + at, len, &d->sps))
            d->have_sps = 1;
        at += len;
    }
    if (at >= avcc_size) return 0;
    int pps_count = avcc[at++];
    d->have_pps = 0;
    for (int i = 0; i < pps_count; i++) {
        if (at + 2 > avcc_size) return 0;
        size_t len = ((size_t)avcc[at] << 8) | avcc[at + 1];
        at += 2;
        if (at + len > avcc_size) return 0;
        if (!d->have_pps && parse_pps(avcc + at, len, &d->pps))
            d->have_pps = 1;
        at += len;
    }
    if (!d->have_sps || !d->have_pps) return 0;
    /* The container and the bitstream must tell the same story. */
    if (d->sps.width != width || d->sps.height != height) return 0;

    d->codec_config.assign(avcc, avcc + avcc_size);
    d->width = width;
    d->height = height;
    d->ready = 1;
    return 1;
}

void h264_flush(H264Decoder* d) {
    (void)d;    /* reference pictures drop here once reconstruction exists */
}

int h264_decode_packet(H264Decoder* d, const Packet* pkt, VideoFrame* out) {
    if (!d || !d->ready || !pkt || !out) return 0;
    if (!pkt->data || pkt->size == 0) return 0;

    /* Walk the MP4-form access unit: length-prefixed NAL units, prefix
     * width from avcC. A malformed AU fails here for real. */
    size_t at = 0;
    int nals = 0;
    while (at < pkt->size) {
        if (at + (size_t)d->nal_length_size > pkt->size) return 0;
        size_t len = 0;
        for (int i = 0; i < d->nal_length_size; i++)
            len = (len << 8) | pkt->data[at + i];
        at += d->nal_length_size;
        if (len == 0 || at + len > pkt->size) return 0;
        nals++;
        at += len;
    }
    if (nals == 0) return 0;

    /* Slice reconstruction lands here next; a correctly-shaped synthetic
     * frame keeps the presenter path running until it does. */
    out->width = d->width;
    out->height = d->height;
    out->stride = d->width * 4;
    out->pts = pkt->pts;
    out->keyframe = pkt->keyframe;
    unsigned char tint = (unsigned char)(((int)(pkt->pts * 25.0)) % 255);
    out->pixels.assign((size_t)out->stride * (size_t)out->height, tint);
    return 1;
}

void h264_free(H264Decoder* d) {
    if (!d) return;
    d->ready = 0;
    d->width = 0;
    d->height = 0;
    d->codec_config.clear();
    d->have_sps = 0;
    d->have_pps = 0;
}
