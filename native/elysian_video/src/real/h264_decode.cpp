#include "h264_decode.h"

int h264_init(H264Decoder* d, const unsigned char* avcc, size_t avcc_size,
              int width, int height) {
    if (!d || !avcc || avcc_size == 0 || width <= 0 || height <= 0)
        return 0;
    d->codec_config.assign(avcc, avcc + avcc_size);
    d->width = width;
    d->height = height;
    d->ready = 1;
    return 1;
}

void h264_flush(H264Decoder* d) {
    (void)d;    /* stateless while synthetic; real decode drops refs here */
}

int h264_decode_packet(H264Decoder* d, const Packet* pkt, VideoFrame* out) {
    if (!d || !d->ready || !pkt || !out) return 0;
    out->width = d->width;
    out->height = d->height;
    out->stride = d->width * 4;
    out->pts = pkt->pts;
    out->keyframe = pkt->keyframe;
    /* Synthetic test pattern: a flat tint derived from the pts, cheap to
     * fill and visibly time-varying once native painting exists. */
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
}
