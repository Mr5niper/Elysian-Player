#include "player.h"
#include "clock.h"
#include "mp4_demux.h"
#include "aac_decode.h"
#include "h264_decode.h"
#include "audio_out.h"
#include "video_out.h"
#include "queue.h"

#include <stdlib.h>
#include <string.h>
#include <wchar.h>

static void clear_info(ElyMediaInfo* info) {
    memset(info, 0, sizeof(*info));
    info->struct_size = (int)sizeof(ElyMediaInfo);
    info->kind = ELY_MEDIA_UNKNOWN;
}

void player_set_error(ElyPlayer* p, const wchar_t* msg) {
    if (!p) return;
    wcsncpy(p->last_error, msg, 255);
    p->last_error[255] = 0;
}

ElyPlayer* player_create(void) {
    ElyPlayer* p = new (std::nothrow) ElyPlayer();
    if (!p) return NULL;
    p->state = ELY_STATE_EMPTY;
    p->volume = 1.0f;
    p->hwnd = NULL;
    p->target_w = p->target_h = 0;
    p->last_error[0] = 0;
    clear_info(&p->info);

    p->clock = new (std::nothrow) PlaybackClock();
    p->demux = new (std::nothrow) Mp4Demux();
    p->audio_dec = new (std::nothrow) AacDecoder();
    p->video_dec = new (std::nothrow) H264Decoder();
    p->audio_out = new (std::nothrow) AudioOut();
    p->video_out = new (std::nothrow) VideoOut();
    p->audio_q = new (std::nothrow) PacketQueue();
    p->video_q = new (std::nothrow) PacketQueue();
    if (!p->clock || !p->demux || !p->audio_dec || !p->video_dec ||
        !p->audio_out || !p->video_out || !p->audio_q || !p->video_q) {
        player_destroy(p);
        return NULL;
    }
    /* no memset over VideoOut: it holds non-trivial members now */
    memset(p->audio_q, 0, sizeof(PacketQueue));
    memset(p->video_q, 0, sizeof(PacketQueue));
    if (!queue_init(p->audio_q, 64) || !queue_init(p->video_q, 64)) {
        player_destroy(p);
        return NULL;
    }
    p->demux_buf.reserve(1 << 20);
    p->demux_eof = 0;
    p->audio_ready = p->video_ready = 0;
    p->audio_eof = p->video_eof = 0;
    p->audio_failures = p->video_failures = 0;
    clock_reset(p->clock, 0.0);
    return p;
}

void player_destroy(ElyPlayer* p) {
    if (!p) return;
    player_reset_pipeline(p);          /* frees queued packet payloads */
    if (p->demux) mp4_close(p->demux);
    if (p->audio_out) audio_out_close(p->audio_out);
    if (p->audio_dec) aac_free(p->audio_dec);
    if (p->video_dec) h264_free(p->video_dec);
    if (p->audio_q) queue_free(p->audio_q);
    if (p->video_q) queue_free(p->video_q);
    delete p->clock;
    delete p->demux;
    delete p->audio_dec;
    delete p->video_dec;
    delete p->audio_out;
    delete p->video_out;
    delete p->audio_q;
    delete p->video_q;
    delete p;
}

void player_reset_pipeline(ElyPlayer* p) {
    if (!p) return;
    if (p->audio_q) queue_clear(p->audio_q);
    if (p->video_q) queue_clear(p->video_q);
    p->demux_eof = 0;
    p->audio_eof = 0;
    p->video_eof = 0;
    p->audio_ready = 0;
    p->video_ready = 0;
    p->audio_failures = 0;
    p->video_failures = 0;
    if (p->audio_out) audio_out_flush(p->audio_out);
    if (p->video_out) video_out_clear(p->video_out);
    if (p->audio_dec) aac_flush(p->audio_dec);
    if (p->video_dec) h264_flush(p->video_dec);
}

int player_prepare_pipeline(ElyPlayer* p) {
    if (!p || !p->demux) return 0;
    if (p->demux->has_audio && !p->audio_ready) {
        if (!aac_init(p->audio_dec,
                      p->demux->audio.codec_config.data(),
                      p->demux->audio.codec_config.size(),
                      p->demux->sample_rate, p->demux->channels))
            return 0;
        if (!audio_out_open(p->audio_out, p->demux->sample_rate,
                            p->demux->channels))
            return 0;
        audio_out_set_volume(p->audio_out, p->volume);
        p->audio_ready = 1;
    }
    if (p->demux->has_video && !p->video_ready) {
        if (!h264_init(p->video_dec,
                       p->demux->video.codec_config.data(),
                       p->demux->video.codec_config.size(),
                       p->demux->width, p->demux->height))
            return 0;
        p->video_ready = 1;
    }
    return 1;
}

int player_fill_queues(ElyPlayer* p, int max_packets) {
    if (!p || !p->demux || p->demux_eof) return 0;
    if (p->demux_buf.size() < (1 << 20))
        p->demux_buf.resize(1 << 20);

    int filled = 0;
    while (filled < max_packets) {
        /* Route before consuming: peek which track is next, check that
         * queue's capacity, and only then advance the demuxer. A full
         * target no longer costs a consumed sample. */
        int kind = 0;
        if (!mp4_peek_next_kind(p->demux, &kind)) {
            p->demux_eof = 1;
            break;
        }
        PacketQueue* target =
            (kind == ELY_MEDIA_AUDIO) ? p->audio_q : p->video_q;
        if (target->count >= target->cap)
            break;

        Mp4Sample s;
        int got_kind = 0;
        if (!mp4_next_sample(p->demux, &s, &got_kind,
                             p->demux_buf.data(), p->demux_buf.size())) {
            p->demux_eof = 1;
            break;
        }
        Packet pkt;
        memset(&pkt, 0, sizeof(pkt));
        pkt.size = s.size;
        pkt.data = (unsigned char*)malloc(s.size ? s.size : 1);
        if (!pkt.data) break;
        memcpy(pkt.data, p->demux_buf.data(), s.size);
        pkt.pts = s.dts;
        pkt.duration = s.duration;
        pkt.stream_kind = got_kind;
        pkt.keyframe = s.keyframe;
        if (!queue_push(target, pkt)) {
            packet_dispose(&pkt);
            break;
        }
        filled++;
    }
    return filled;
}

int player_pipeline_drained(const ElyPlayer* p) {
    if (!p) return 0;
    return p->demux_eof
        && p->audio_q->count == 0
        && p->video_q->count == 0
        && (!p->demux->has_audio || p->audio_eof)
        && (!p->demux->has_video || p->video_eof);
}

/* Stepped pipeline pump, paced by the playback clock: decode only what is
 * due within a small horizon so the engine plays through media in real
 * time, bounded per call so ABI getters stay non-blocking. Once the clock
 * reaches the duration the horizon opens to drain the tail, still under
 * the bound; if one call cannot finish the tail, the next one does. */
int player_pump(ElyPlayer* p) {
    if (!p || p->state != ELY_STATE_PLAYING) return 0;
    if (!player_prepare_pipeline(p)) return -1;

    double now = clock_position(p->clock, p->info.duration, 0);
    double horizon = now + 0.5;
    if (p->info.duration > 0.0 && now >= p->info.duration)
        horizon = 1e30;                     /* drain the tail */

    int work = 0;
    int guard = 2048;
    while (guard-- > 0) {
        if (!p->demux_eof &&
            p->audio_q->count + p->video_q->count < 32)
            player_fill_queues(p, 16);

        int did = 0;
        if (p->audio_q->count > 0 &&
            p->audio_q->items[p->audio_q->head].pts <= horizon) {
            Packet pkt;
            queue_pop(p->audio_q, &pkt);
            PcmFrame pcm;
            if (aac_decode_packet(p->audio_dec, &pkt, &pcm)) {
                audio_out_write(p->audio_out, &pcm);
                p->audio_failures = 0;
            } else if (++p->audio_failures > 8) {
                packet_dispose(&pkt);
                return -1;
            }
            packet_dispose(&pkt);
            did = 1;
            work++;
        }
        if (p->video_q->count > 0 &&
            p->video_q->items[p->video_q->head].pts <= horizon) {
            Packet pkt;
            queue_pop(p->video_q, &pkt);
            VideoFrame frame;
            if (h264_decode_packet(p->video_dec, &pkt, &frame)) {
                video_out_present(p->video_out, &frame);
                p->video_failures = 0;
            } else if (++p->video_failures > 8) {
                packet_dispose(&pkt);
                return -1;
            }
            packet_dispose(&pkt);
            did = 1;
            work++;
        }
        if (!did) {
            if (p->demux_eof) {
                if (p->audio_q->count == 0) p->audio_eof = 1;
                if (p->video_q->count == 0) p->video_eof = 1;
            }
            break;
        }
    }
    return work;
}

void player_settle(ElyPlayer* p) {
    if (!p || p->state != ELY_STATE_PLAYING) return;
    if (player_pump(p) < 0) {
        /* Repeated decode failure is fatal for this media, per contract:
         * the player promotes to ERROR and unload recovers it. */
        player_set_error(p, L"decode: pipeline failure");
        p->state = ELY_STATE_ERROR;
        return;
    }
    /* Audio is master only when a real device clock exists; the synthetic
     * sink reports none and the playback clock stays authoritative, which
     * is what keeps wall-time position semantics intact until WASAPI. */
    double device = audio_out_position(p->audio_out);
    double pos = device >= 0.0
        ? device
        : clock_position(p->clock, p->info.duration, 0);
    if (p->info.duration > 0.0 && pos >= p->info.duration &&
        player_pipeline_drained(p)) {
        clock_pause(p->clock);
        clock_seek(p->clock, p->info.duration);
        p->state = ELY_STATE_ENDED;
    }
}
