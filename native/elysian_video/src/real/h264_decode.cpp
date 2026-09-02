#include "h264_decode.h"
#include "bitreader.h"

#include <string.h>

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
    br_read(&br, 8);
    sps->level_idc = (int)br_read(&br, 8);
    br_ue(&br);

    if (sps->profile_idc == 100 || sps->profile_idc == 110 ||
        sps->profile_idc == 122 || sps->profile_idc == 244 ||
        sps->profile_idc == 44 || sps->profile_idc == 83 ||
        sps->profile_idc == 86 || sps->profile_idc == 118 ||
        sps->profile_idc == 128) {
        uint32_t chroma = br_ue(&br);
        if (chroma == 3) br_read1(&br);
        br_ue(&br);
        br_ue(&br);
        br_read1(&br);
        if (br_read1(&br)) {
            int lists = chroma == 3 ? 12 : 8;
            for (int i = 0; i < lists; i++) {
                if (br_read1(&br)) {
                    int size_of = i < 6 ? 16 : 64;
                    int last = 8, next = 8;
                    for (int j = 0; j < size_of; j++) {
                        if (next) next = (last + br_se(&br) + 256) % 256;
                        last = next ? next : last;
                    }
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

    br_ue(&br);
    br_read1(&br);

    sps->mb_width = (int)br_ue(&br) + 1;
    sps->mb_height = (int)br_ue(&br) + 1;
    sps->frame_mbs_only = (int)br_read1(&br);
    if (!sps->frame_mbs_only) br_read1(&br);
    br_read1(&br);

    int crop_l = 0, crop_r = 0, crop_t = 0, crop_b = 0;
    if (br_read1(&br)) {
        crop_l = (int)br_ue(&br);
        crop_r = (int)br_ue(&br);
        crop_t = (int)br_ue(&br);
        crop_b = (int)br_ue(&br);
    }
    if (!br_ok(&br)) return 0;

    int height_map = sps->mb_height * (sps->frame_mbs_only ? 1 : 2);
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
    br_ue(&br);
    br_ue(&br);
    pps->entropy_cabac = (int)br_read1(&br);
    br_read1(&br);
    uint32_t slice_groups = br_ue(&br) + 1;
    if (slice_groups > 1) return 0;
    br_ue(&br);
    br_ue(&br);
    br_read1(&br);
    br_read(&br, 2);
    pps->pic_init_qp = 26 + br_se(&br);
    return br_ok(&br);
}

static int parse_slice_header(BitReader* br, H264Decoder* d,
                              int nal_type, int* out_slice_type,
                              int* out_frame_num, int* out_poc_lsb) {
    br_ue(br);                                /* first_mb_in_slice */
    int slice_type = (int)br_ue(br);
    br_ue(br);                                /* pps_id */
    int frame_num = (int)br_read(br, d->sps.log2_max_frame_num);
    if (!br_ok(br)) return 0;

    int poc_lsb = 0;
    if (d->sps.poc_type == 0) {
        poc_lsb = (int)br_read(br, d->sps.log2_max_poc_lsb);
        if (!br_ok(br)) return 0;
    }

    if (nal_type == 5) br_ue(br);             /* idr_pic_id */

    *out_slice_type = slice_type % 5;
    *out_frame_num = frame_num;
    *out_poc_lsb = poc_lsb;
    return 1;
}

static void synth_from_slice_structure(H264Decoder* d, VideoFrame* out,
                                       int slice_type, int frame_num,
                                       int poc_lsb, int idr) {
    out->width = d->width;
    out->height = d->height;
    out->stride = d->width * 4;
    out->pixels.resize((size_t)out->stride * (size_t)out->height);

    unsigned char border_r = idr ? 255 : 60;
    unsigned char border_g = idr ? 50 : 180;
    unsigned char border_b = idr ? 50 : 255;

    for (int y = 0; y < out->height; y++) {
        unsigned char* row = out->pixels.data() + (size_t)y * (size_t)out->stride;
        for (int x = 0; x < out->width; x++) {
            int border = (x < 8 || y < 8 || x >= out->width - 8 || y >= out->height - 8);

            unsigned char r = border ? border_r : (unsigned char)((x + frame_num * 7) & 0xFF);
            unsigned char g = border ? border_g : (unsigned char)((y + poc_lsb * 3) & 0xFF);
            unsigned char b = border ? border_b : (unsigned char)(((slice_type + 1) * 40 + x / 8 + y / 8) & 0xFF);

            row[x * 4 + 0] = r;
            row[x * 4 + 1] = g;
            row[x * 4 + 2] = b;
            row[x * 4 + 3] = 255;
        }
    }
}

int h264_init(H264Decoder* d, const unsigned char* avcc, size_t avcc_size,
              int width, int height) {
    if (!d || !avcc || avcc_size < 7 || width <= 0 || height <= 0)
        return 0;
    if (avcc[0] != 1) return 0;
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
    if (d->sps.width != width || d->sps.height != height) return 0;

    d->codec_config.assign(avcc, avcc + avcc_size);
    d->width = width;
    d->height = height;
    d->frame_num = 0;
    d->poc_lsb = 0;
    d->last_slice_type = -1;
    d->seen_idr = 0;
    d->ready = 1;
    return 1;
}

void h264_flush(H264Decoder* d) {
    if (!d) return;
    d->frame_num = 0;
    d->poc_lsb = 0;
    d->last_slice_type = -1;
    d->seen_idr = 0;
}

int h264_decode_packet(H264Decoder* d, const Packet* pkt, VideoFrame* out) {
    if (!d || !d->ready || !pkt || !out) return 0;
    if (!pkt->data || pkt->size == 0) return 0;

    size_t at = 0;
    int found_slice = 0;
    int slice_type = -1;
    int frame_num = 0;
    int poc_lsb = 0;
    int idr = 0;

    while (at < pkt->size) {
        if (at + (size_t)d->nal_length_size > pkt->size) return 0;
        size_t len = 0;
        for (int i = 0; i < d->nal_length_size; i++)
            len = (len << 8) | pkt->data[at + i];
        at += d->nal_length_size;
        if (len == 0 || at + len > pkt->size) return 0;

        const unsigned char* nal = pkt->data + at;
        int nal_type = nal[0] & 0x1F;
        int nal_ref_idc = (nal[0] >> 5) & 0x03;
        (void)nal_ref_idc;

        if (nal_type == 1 || nal_type == 5) {
            std::vector<unsigned char> rbsp;
            unescape_rbsp(nal + 1, len - 1, &rbsp);
            BitReader br;
            br_init(&br, rbsp.data(), rbsp.size());
            if (!parse_slice_header(&br, d, nal_type, &slice_type, &frame_num, &poc_lsb))
                return 0;
            found_slice = 1;
            idr = (nal_type == 5);
            break;
        }

        at += len;
    }

    if (!found_slice) return 0;

    d->frame_num = frame_num;
    d->poc_lsb = poc_lsb;
    d->last_slice_type = slice_type;
    if (idr) d->seen_idr = 1;

    out->pts = pkt->pts;
    out->keyframe = pkt->keyframe || idr;
    synth_from_slice_structure(d, out, slice_type, frame_num, poc_lsb, idr);
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
    d->frame_num = 0;
    d->poc_lsb = 0;
    d->last_slice_type = -1;
    d->seen_idr = 0;
}
