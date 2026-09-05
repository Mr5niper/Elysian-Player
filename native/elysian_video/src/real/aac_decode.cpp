#include "aac_decode.h"

extern "C" {
#include <libavcodec/avcodec.h>
#include <libavutil/avutil.h>
#include <libavutil/opt.h>
#include <libswresample/swresample.h>
}

#include <string.h>
#include <cmath>

/* AVCodecContext::channels/channel_layout were removed in newer FFmpeg in
 * favor of AVChannelLayout; both this sandbox's 6.1 and any recent Windows
 * LGPL shared build use the new field, so only that path is written. */

int aac_init(AacDecoder* d, const unsigned char* asc, size_t asc_size,
             int sample_rate, int channels) {
    if (!d) return 0;
    aac_free(d);
    if (sample_rate <= 0 || channels <= 0) return 0;

    const AVCodec* codec = avcodec_find_decoder(AV_CODEC_ID_AAC);
    if (!codec) return 0;
    AVCodecContext* ctx = avcodec_alloc_context3(codec);
    if (!ctx) return 0;

    ctx->sample_rate = sample_rate;
    av_channel_layout_default(&ctx->ch_layout, channels);
    /* Same reasoning as h264_decode.cpp: packets are fed manually, so
     * pkt_timebase must be set before open or frame pts comes back
     * meaningless. */
    ctx->pkt_timebase = AVRational{1, 1000000};

    if (asc && asc_size > 0) {
        ctx->extradata = (uint8_t*)av_mallocz(
            asc_size + AV_INPUT_BUFFER_PADDING_SIZE);
        if (!ctx->extradata) { avcodec_free_context(&ctx); return 0; }
        memcpy(ctx->extradata, asc, asc_size);
        ctx->extradata_size = (int)asc_size;
    }

    if (avcodec_open2(ctx, codec, nullptr) < 0) {
        avcodec_free_context(&ctx);
        return 0;
    }

    AVFrame* frame = av_frame_alloc();
    if (!frame) { avcodec_free_context(&ctx); return 0; }

    d->ctx = ctx;
    d->frame = frame;
    d->swr = nullptr;   /* built lazily once the first real frame arrives,
                          * when the decoder's actual output layout is
                          * known rather than assumed from the container. */
    d->ready = 1;
    d->sample_rate = sample_rate;
    d->channels = channels;
    return 1;
}

void aac_flush(AacDecoder* d) {
    if (!d || !d->ctx) return;
    avcodec_flush_buffers(static_cast<AVCodecContext*>(d->ctx));
}

static int ensure_swr(AacDecoder* d, AVFrame* f) {
    if (d->swr) return 1;
    SwrContext* swr = nullptr;
    AVChannelLayout out_layout;
    av_channel_layout_default(&out_layout, d->channels);
    int rc = swr_alloc_set_opts2(
        &swr, &out_layout, AV_SAMPLE_FMT_FLT, d->sample_rate,
        &f->ch_layout, (AVSampleFormat)f->format, f->sample_rate,
        0, nullptr);
    av_channel_layout_uninit(&out_layout);
    if (rc < 0 || !swr) return 0;
    if (swr_init(swr) < 0) { swr_free(&swr); return 0; }
    d->swr = swr;
    return 1;
}

int aac_decode_packet(AacDecoder* d, const Packet* pkt, PcmFrame* out) {
    if (!d || !d->ready || !out) return -1;
    AVCodecContext* ctx = static_cast<AVCodecContext*>(d->ctx);
    AVFrame* frame = static_cast<AVFrame*>(d->frame);

    if (pkt) {
        if (pkt->data.empty())
            return -1;   /* explicit reject: zero-byte input is never valid */
        AVPacket* avpkt = av_packet_alloc();
        if (!avpkt) return -1;
        if (av_new_packet(avpkt, (int)pkt->data.size()) < 0) {
            av_packet_free(&avpkt);
            return -1;
        }
        memcpy(avpkt->data, pkt->data.data(), pkt->data.size());
        avpkt->pts = (int64_t)llround(pkt->pts * 1000000.0);
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

    if (!ensure_swr(d, frame)) return -1;
    SwrContext* swr = static_cast<SwrContext*>(d->swr);

    int max_out = frame->nb_samples +
        swr_get_delay(swr, frame->sample_rate) * d->sample_rate /
        frame->sample_rate + 32;
    out->samples.assign((size_t)max_out * d->channels, 0.0f);
    uint8_t* out_planes[1] = {
        reinterpret_cast<uint8_t*>(out->samples.data()) };
    int converted = swr_convert(swr, out_planes, max_out,
                                (const uint8_t**)frame->extended_data,
                                frame->nb_samples);
    if (converted < 0) { out->samples.clear(); return -1; }

    out->frames = (size_t)converted;
    out->channels = d->channels;
    out->sample_rate = d->sample_rate;
    out->samples.resize((size_t)converted * d->channels);
    /* Audio decode order equals presentation order, so no reordering ever
     * applies; frame->pts (microsecond pkt_timebase) is authoritative when
     * present, and the last packet sent is the honest fallback rather than
     * reporting 0.0 at a flush boundary. */
    out->pts = (frame->pts != AV_NOPTS_VALUE)
        ? (double)frame->pts / 1000000.0 : d->last_sent_pts;
    return 1;
}

void aac_free(AacDecoder* d) {
    if (!d) return;
    if (d->swr) {
        SwrContext* swr = static_cast<SwrContext*>(d->swr);
        swr_free(&swr);
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
    d->swr = nullptr;
    d->ready = 0;
}
