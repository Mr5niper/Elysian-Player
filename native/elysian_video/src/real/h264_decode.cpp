#include "h264_decode.h"

extern "C" {
#include <libavcodec/avcodec.h>
#include <libavutil/avutil.h>
#include <libswscale/swscale.h>
}

#include <string.h>
#include <cmath>

int h264_init(H264Decoder* d, const unsigned char* avcc, size_t avcc_size,
              int width, int height) {
    if (!d) return 0;
    h264_free(d);
    if (width <= 0 || height <= 0) return 0;

    const AVCodec* codec = avcodec_find_decoder(AV_CODEC_ID_H264);
    if (!codec) return 0;
    AVCodecContext* ctx = avcodec_alloc_context3(codec);
    if (!ctx) return 0;

    ctx->width = width;
    ctx->height = height;
    /* Packets are fed manually here (copied out of this engine's own
     * queue, not read straight off an AVFormatContext), so nothing tells
     * the decoder how to interpret packet/frame pts unless this is set
     * before opening. Microseconds gives seek-target precision without
     * float error, and pkt->pts (already in seconds, per mp4_next_sample)
     * converts to it losslessly enough for playback timing. */
    ctx->pkt_timebase = AVRational{1, 1000000};
    if (avcc && avcc_size > 0) {
        ctx->extradata = (uint8_t*)av_mallocz(
            avcc_size + AV_INPUT_BUFFER_PADDING_SIZE);
        if (!ctx->extradata) { avcodec_free_context(&ctx); return 0; }
        memcpy(ctx->extradata, avcc, avcc_size);
        ctx->extradata_size = (int)avcc_size;
    }

    if (avcodec_open2(ctx, codec, nullptr) < 0) {
        avcodec_free_context(&ctx);
        return 0;
    }

    AVFrame* frame = av_frame_alloc();
    if (!frame) { avcodec_free_context(&ctx); return 0; }

    d->ctx = ctx;
    d->frame = frame;
    d->sws = nullptr;   /* built lazily once the decoder reports its real
                          * output geometry and pixel format */
    d->ready = 1;
    d->width = width;
    d->height = height;
    return 1;
}

void h264_flush(H264Decoder* d) {
    if (!d || !d->ctx) return;
    avcodec_flush_buffers(static_cast<AVCodecContext*>(d->ctx));
}

static int ensure_sws(H264Decoder* d, AVFrame* f) {
    SwsContext* sws = static_cast<SwsContext*>(d->sws);
    if (sws) return 1;
    sws = sws_getContext(f->width, f->height, (AVPixelFormat)f->format,
                         d->width, d->height, AV_PIX_FMT_BGRA,
                         SWS_BILINEAR, nullptr, nullptr, nullptr);
    if (!sws) return 0;
    d->sws = sws;
    return 1;
}

int h264_decode_packet(H264Decoder* d, const Packet* pkt, VideoFrame* out) {
    if (!d || !d->ready || !out) return -1;
    AVCodecContext* ctx = static_cast<AVCodecContext*>(d->ctx);
    AVFrame* frame = static_cast<AVFrame*>(d->frame);

    if (pkt) {
        if (!pkt->data || pkt->size == 0)
            return -1;   /* explicit reject: zero-byte input is never valid */
        AVPacket* avpkt = av_packet_alloc();
        if (!avpkt) return -1;
        if (av_new_packet(avpkt, (int)pkt->size) < 0) {
            av_packet_free(&avpkt);
            return -1;
        }
        memcpy(avpkt->data, pkt->data, pkt->size);
        avpkt->pts = (int64_t)llround(pkt->pts * 1000000.0);
        avpkt->flags = pkt->keyframe ? AV_PKT_FLAG_KEY : 0;
        d->last_sent_pts = pkt->pts;
        int send_rc = avcodec_send_packet(ctx, avpkt);
        av_packet_free(&avpkt);
        if (send_rc < 0 && send_rc != AVERROR(EAGAIN))
            return -1;      /* rejected: bad data */
    } else {
        avcodec_send_packet(ctx, nullptr);   /* EOF drain signal */
    }

    int rc = avcodec_receive_frame(ctx, frame);
    if (rc == AVERROR(EAGAIN)) return 0;     /* accepted, nothing ready yet */
    if (rc == AVERROR_EOF) return 0;         /* fully drained */
    if (rc < 0) return -1;

    if (!ensure_sws(d, frame)) return -1;
    SwsContext* sws = static_cast<SwsContext*>(d->sws);

    out->width = d->width;
    out->height = d->height;
    out->stride = d->width * 4;
    out->pixels.assign((size_t)out->stride * d->height, 0);
    uint8_t* dst[1] = { out->pixels.data() };
    int dst_stride[1] = { out->stride };
    sws_scale(sws, frame->data, frame->linesize, 0, frame->height,
             dst, dst_stride);

    /* best_effort_timestamp is the reordered presentation time B-frames
     * require; it comes back in the microsecond pkt_timebase set at open,
     * so seconds = ticks / 1e6. The rare frame with no usable timestamp
     * (a flush-boundary edge case) falls back to the last packet sent
     * rather than reporting 0.0, which would show as a timeline glitch. */
    out->pts = (frame->best_effort_timestamp != AV_NOPTS_VALUE)
        ? (double)frame->best_effort_timestamp / 1000000.0
        : d->last_sent_pts;
    out->keyframe = (frame->flags & AV_FRAME_FLAG_KEY) ? 1 : 0;
    return 1;
}

void h264_free(H264Decoder* d) {
    if (!d) return;
    if (d->sws) {
        SwsContext* s = static_cast<SwsContext*>(d->sws);
        sws_freeContext(s);
    }
    if (d->frame) {
        AVFrame* f = static_cast<AVFrame*>(d->frame);
        av_frame_free(&f);
    }
    if (d->ctx) {
        AVCodecContext* c = static_cast<AVCodecContext*>(d->ctx);
        avcodec_free_context(&c);
    }
    d->ctx = nullptr;
    d->frame = nullptr;
    d->sws = nullptr;
    d->ready = 0;
}
