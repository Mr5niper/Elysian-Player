#include "mp4_demux.h"

extern "C" {
#include <libavformat/avformat.h>
#include <libavutil/avutil.h>
}

#include <string.h>
#include <string>

#ifdef _WIN32
#include <windows.h>
#endif

/* ---- path conversion --------------------------------------------------
 * avformat_open_input wants a UTF-8 path (its Windows file protocol
 * converts back to UTF-16 internally); the ABI hands us wchar_t*. */
static std::string wide_to_utf8(const wchar_t* w) {
#ifdef _WIN32
    if (!w || !w[0]) return std::string();
    int need = WideCharToMultiByte(CP_UTF8, 0, w, -1, NULL, 0, NULL, NULL);
    if (need <= 0) return std::string();
    std::string out(need - 1, '\0');
    WideCharToMultiByte(CP_UTF8, 0, w, -1, out.data(), need, NULL, NULL);
    return out;
#else
    /* Linux test builds: wchar_t is UCS-4, so each element is already a
     * Unicode code point (unlike Windows' UTF-16 wchar_t). The contract
     * test's unicode-filename case exercises exactly this path. */
    std::string out;
    for (const wchar_t* c = w; c && *c; ++c) {
        uint32_t cp = (uint32_t)*c;
        if (cp < 0x80) {
            out.push_back((char)cp);
        } else if (cp < 0x800) {
            out.push_back((char)(0xC0 | (cp >> 6)));
            out.push_back((char)(0x80 | (cp & 0x3F)));
        } else if (cp < 0x10000) {
            out.push_back((char)(0xE0 | (cp >> 12)));
            out.push_back((char)(0x80 | ((cp >> 6) & 0x3F)));
            out.push_back((char)(0x80 | (cp & 0x3F)));
        } else {
            out.push_back((char)(0xF0 | (cp >> 18)));
            out.push_back((char)(0x80 | ((cp >> 12) & 0x3F)));
            out.push_back((char)(0x80 | ((cp >> 6) & 0x3F)));
            out.push_back((char)(0x80 | (cp & 0x3F)));
        }
    }
    return out;
#endif
}

static void free_track(Mp4Track& t) {
    t = Mp4Track();
}

void mp4_close(Mp4Demux* d) {
    if (!d) return;
    if (d->pending_pkt) {
        av_packet_free(reinterpret_cast<AVPacket**>(&d->pending_pkt));
        d->pending_pkt = nullptr;
    }
    if (d->fmt_ctx) {
        AVFormatContext* fmt = static_cast<AVFormatContext*>(d->fmt_ctx);
        avformat_close_input(&fmt);
        d->fmt_ctx = nullptr;
    }
    d->audio_stream_index = -1;
    d->video_stream_index = -1;
    d->pending_kind = 0;
    d->duration = 0.0;
    d->frame_rate = 0.0;
    d->has_audio = d->has_video = 0;
    d->width = d->height = 0;
    d->sample_rate = d->channels = 0;
    free_track(d->audio);
    free_track(d->video);
}

int mp4_open(Mp4Demux* d, const wchar_t* path) {
    if (!d || !path || !path[0]) return ELY_ERR_BAD_ARG;
    mp4_close(d);

    std::string utf8 = wide_to_utf8(path);
    if (utf8.empty()) return ELY_ERR_BAD_ARG;

    /* File existence is checked directly rather than inferred from
     * avformat's error code, which is not guaranteed to be ENOENT for
     * every "no such file" case across platforms/protocols. */
    FILE* probe_fp = fopen(utf8.c_str(), "rb");
    if (!probe_fp) return ELY_ERR_NOT_FOUND;

    /* Probe the container signature BEFORE a full open, so a truncated or
     * corrupt MP4 (whose ftyp signature still matches the family) can be
     * told apart from a file that was never MP4-family at all. Without
     * this, avformat_open_input's single pass/fail collapses both into
     * the same "won't open" outcome, and a damaged real file would
     * misreport as UNSUPPORTED instead of BAD_CONTAINER. */
    unsigned char probe_buf[4096];
    size_t probe_len = fread(probe_buf, 1, sizeof(probe_buf), probe_fp);
    fclose(probe_fp);

    AVProbeData pd;
    memset(&pd, 0, sizeof(pd));
    pd.buf = probe_buf;
    pd.buf_size = (int)probe_len;
    pd.filename = utf8.c_str();
    const AVInputFormat* probed = av_probe_input_format(&pd, 1);
    bool looks_like_mp4_family = probed && probed->name &&
        strcmp(probed->name, "mov,mp4,m4a,3gp,3g2,mj2") == 0;
    if (!looks_like_mp4_family)
        return ELY_ERR_UNSUPPORTED;

    AVFormatContext* fmt = nullptr;
    int rc = avformat_open_input(&fmt, utf8.c_str(), nullptr, nullptr);
    if (rc < 0) {
        /* The signature matched the family above, so a failure here means
         * the container itself is damaged (truncated, corrupt boxes), not
         * that this was never an MP4-family file. */
        return ELY_ERR_BAD_CONTAINER;
    }

    if (avformat_find_stream_info(fmt, nullptr) < 0) {
        avformat_close_input(&fmt);
        return ELY_ERR_BAD_CONTAINER;
    }

    int a_idx = av_find_best_stream(fmt, AVMEDIA_TYPE_AUDIO, -1, -1,
                                    nullptr, 0);
    int v_idx = av_find_best_stream(fmt, AVMEDIA_TYPE_VIDEO, -1, -1,
                                    nullptr, 0);

    /* Only AAC audio and H.264 video are playable behind this ABI: the
     * decoders behind it are scoped to those two codecs. A track present
     * in a valid container but in some other codec is treated as if it
     * were absent, not as a hard failure - the other track can still
     * play. */
    if (a_idx >= 0 &&
        fmt->streams[a_idx]->codecpar->codec_id != AV_CODEC_ID_AAC)
        a_idx = -1;
    if (v_idx >= 0 &&
        fmt->streams[v_idx]->codecpar->codec_id != AV_CODEC_ID_H264)
        v_idx = -1;

    if (a_idx < 0 && v_idx < 0) {
        avformat_close_input(&fmt);
        return ELY_ERR_BAD_STREAM;
    }

    d->fmt_ctx = fmt;
    d->audio_stream_index = a_idx;
    d->video_stream_index = v_idx;
    d->has_audio = a_idx >= 0;
    d->has_video = v_idx >= 0;

    double container_duration = 0.0;
    if (fmt->duration != AV_NOPTS_VALUE)
        container_duration = (double)fmt->duration / (double)AV_TIME_BASE;

    if (d->has_audio) {
        AVStream* s = fmt->streams[a_idx];
        AVCodecParameters* cp = s->codecpar;
        d->audio.present = 1;
        d->audio.sample_rate = cp->sample_rate;
#if LIBAVUTIL_VERSION_MAJOR >= 57
        d->audio.channels = cp->ch_layout.nb_channels;
#else
        d->audio.channels = cp->channels;
#endif
        if (cp->extradata && cp->extradata_size > 0)
            d->audio.codec_config.assign(
                cp->extradata, cp->extradata + cp->extradata_size);
        double sd = (s->duration != AV_NOPTS_VALUE)
            ? s->duration * av_q2d(s->time_base) : 0.0;
        d->audio.duration = sd;
        d->sample_rate = d->audio.sample_rate;
        d->channels = d->audio.channels;
    }
    if (d->has_video) {
        AVStream* s = fmt->streams[v_idx];
        AVCodecParameters* cp = s->codecpar;
        d->video.present = 1;
        d->video.width = cp->width;
        d->video.height = cp->height;
        if (cp->extradata && cp->extradata_size > 0)
            d->video.codec_config.assign(
                cp->extradata, cp->extradata + cp->extradata_size);
        double sd = (s->duration != AV_NOPTS_VALUE)
            ? s->duration * av_q2d(s->time_base) : 0.0;
        d->video.duration = sd;
        d->width = d->video.width;
        d->height = d->video.height;
        AVRational fr = av_guess_frame_rate(fmt, s, nullptr);
        d->frame_rate = (fr.num && fr.den) ? av_q2d(fr) : 0.0;
    }

    d->duration = container_duration;
    if (d->duration <= 0.0) {
        /* Some encoders omit the container-level duration; fall back to
         * whichever stream reported one. */
        if (d->has_audio && d->audio.duration > 0.0)
            d->duration = d->audio.duration;
        else if (d->has_video && d->video.duration > 0.0)
            d->duration = d->video.duration;
    }

    return ELY_OK;
}

int mp4_fill_info(Mp4Demux* d, ElyMediaInfo* out) {
    if (!d || !out) return 0;
    out->has_audio = d->has_audio;
    out->has_video = d->has_video;
    out->width = d->width;
    out->height = d->height;
    out->duration = d->duration;
    out->frame_rate = d->frame_rate;
    out->audio_sample_rate = d->sample_rate;
    out->audio_channels = d->channels;
    out->kind = d->has_video ? ELY_MEDIA_VIDEO
              : d->has_audio ? ELY_MEDIA_AUDIO
              : ELY_MEDIA_UNKNOWN;
    return 1;
}

int mp4_seek(Mp4Demux* d, double seconds) {
    if (!d || !d->fmt_ctx) return 0;
    AVFormatContext* fmt = static_cast<AVFormatContext*>(d->fmt_ctx);
    if (d->pending_pkt) {
        av_packet_free(reinterpret_cast<AVPacket**>(&d->pending_pkt));
        d->pending_pkt = nullptr;
    }
    /* Seek on whichever stream exists; video governs keyframe alignment
     * when both tracks are present, matching the owned demuxer's rule
     * that the video cursor snaps to the nearest sync sample. */
    int stream_idx = d->video_stream_index >= 0 ? d->video_stream_index
                    : d->audio_stream_index;
    if (stream_idx < 0) return 0;
    AVStream* s = fmt->streams[stream_idx];
    int64_t target = (int64_t)(seconds / av_q2d(s->time_base));
    int rc = av_seek_frame(fmt, stream_idx, target, AVSEEK_FLAG_BACKWARD);
    if (rc < 0) return 0;
    /* Flush FFmpeg's own internal demux-side buffering for every stream,
     * not just the one seeked on, or a stale packet from before the seek
     * can surface on the other track. */
    for (unsigned i = 0; i < fmt->nb_streams; i++)
        avformat_flush(fmt);
    return 1;
}

/* Reads one packet from whichever stream index this demux tracks (audio or
 * video), classifying it and discarding anything from a track this demux
 * is not using (a container can hold more streams than the two selected at
 * open). Returns 0 at genuine end of file. */
static int read_one(Mp4Demux* d, AVPacket* pkt, int* out_kind) {
    AVFormatContext* fmt = static_cast<AVFormatContext*>(d->fmt_ctx);
    for (;;) {
        int rc = av_read_frame(fmt, pkt);
        if (rc < 0) return 0;
        if (pkt->stream_index == d->audio_stream_index) {
            *out_kind = ELY_MEDIA_AUDIO;
            return 1;
        }
        if (pkt->stream_index == d->video_stream_index) {
            *out_kind = ELY_MEDIA_VIDEO;
            return 1;
        }
        av_packet_unref(pkt);   /* a track we are not playing */
    }
}

int mp4_peek_next_kind(Mp4Demux* d, int* out_kind) {
    if (!d || !d->fmt_ctx) return 0;
    if (d->pending_pkt) {
        *out_kind = d->pending_kind;
        return 1;
    }
    AVPacket* pkt = av_packet_alloc();
    if (!pkt) return 0;
    int kind = 0;
    if (!read_one(d, pkt, &kind)) {
        av_packet_free(&pkt);
        return 0;
    }
    d->pending_pkt = pkt;
    d->pending_kind = kind;
    *out_kind = kind;
    return 1;
}

int mp4_next_sample(Mp4Demux* d, int* out_kind, uint8_t* buf, size_t buf_cap,
                    size_t* out_size, double* out_pts, double* out_duration,
                    int* out_keyframe) {
    if (!d || !d->fmt_ctx) return 0;
    AVFormatContext* fmt = static_cast<AVFormatContext*>(d->fmt_ctx);
    AVPacket* pkt;
    bool owned_here = false;
    if (d->pending_pkt) {
        pkt = static_cast<AVPacket*>(d->pending_pkt);
    } else {
        pkt = av_packet_alloc();
        if (!pkt) return 0;
        int kind = 0;
        if (!read_one(d, pkt, &kind)) {
            av_packet_free(&pkt);
            return 0;
        }
        d->pending_kind = kind;
        owned_here = true;
    }

    if ((size_t)pkt->size > buf_cap) {
        av_packet_free(&pkt);
        d->pending_pkt = nullptr;
        return 0;
    }

    *out_kind = d->pending_kind;
    memcpy(buf, pkt->data, (size_t)pkt->size);
    *out_size = (size_t)pkt->size;

    AVStream* s = fmt->streams[pkt->stream_index];
    double tb = av_q2d(s->time_base);
    int64_t ts = (pkt->pts != AV_NOPTS_VALUE) ? pkt->pts : pkt->dts;
    *out_pts = (ts != AV_NOPTS_VALUE) ? ts * tb : 0.0;
    *out_duration = (pkt->duration > 0) ? pkt->duration * tb : 0.0;
    *out_keyframe = (pkt->flags & AV_PKT_FLAG_KEY) ? 1 : 0;

    (void)owned_here;
    av_packet_free(&pkt);
    d->pending_pkt = nullptr;
    return 1;
}
