#include "player.h"
#include "clock.h"
#include "mp4_demux.h"
#include "aac_decode.h"
#include "h264_decode.h"
#include "audio_out.h"
#include "video_out.h"
#include "queue.h"

#include <chrono>
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

std::unique_lock<std::mutex> player_guard(ElyPlayer* p) {
    return std::unique_lock<std::mutex>(p->ctl_mu);
}

/* Called only while holding ctl_mu: by the pump thread once per settle-and-
 * publish cycle, and by every mutating ABI call right before it returns, so
 * a getter called immediately afterward on any thread sees the fresh value
 * rather than waiting for the pump thread's own next wake. */
void player_publish_locked(ElyPlayer* p) {
    p->pub_state.store(p->state, std::memory_order_relaxed);
    p->pub_playing.store(p->state == ELY_STATE_PLAYING ? 1 : 0,
                         std::memory_order_relaxed);
    p->pub_paused.store(p->state == ELY_STATE_PAUSED ? 1 : 0,
                        std::memory_order_relaxed);
    p->pub_finished.store(p->state == ELY_STATE_ENDED ? 1 : 0,
                          std::memory_order_relaxed);
    double pos = 0.0;
    if (p->state != ELY_STATE_EMPTY && p->state != ELY_STATE_STOPPED)
        pos = clock_position(p->clock, p->info.duration, 1);
    p->pub_position.store(pos, std::memory_order_relaxed);
}

/* Runs forever on its own thread, from player_create() to player_destroy().
 * Sleeps (lock released) between steps via wait_for, so a caller's
 * mutating ABI call is never blocked longer than one in-flight settle
 * pass. 5ms while playing keeps the existing horizon-paced pump in
 * player_pump() well fed without spinning; 50ms while idle costs nothing
 * anyone would notice and keeps last_error/state visible promptly after
 * an external change. */
static void player_thread_main(ElyPlayer* p) {
    std::unique_lock<std::mutex> lock(p->ctl_mu);
    while (!p->stop_requested) {
        if (p->state == ELY_STATE_PLAYING) {
            player_settle(p);
            player_publish_locked(p);
            p->ctl_cv.wait_for(lock, std::chrono::milliseconds(5));
        } else {
            player_publish_locked(p);
            p->ctl_cv.wait_for(lock, std::chrono::milliseconds(50));
        }
    }
}

static void player_start_thread(ElyPlayer* p) {
    p->stop_requested = false;
    p->pump_thread = std::thread(player_thread_main, p);
}

static void player_stop_thread(ElyPlayer* p) {
    {
        std::lock_guard<std::mutex> lock(p->ctl_mu);
        p->stop_requested = true;
    }
    p->ctl_cv.notify_all();
    if (p->pump_thread.joinable())
        p->pump_thread.join();
}

ElyPlayer* player_create(void) {
    ElyPlayer* p = new (std::nothrow) ElyPlayer();
    if (!p) return NULL;
    p->state = ELY_STATE_EMPTY;
    p->pub_volume.store(1.0f, std::memory_order_relaxed);
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
    player_publish_locked(p);   /* no lock needed yet: nothing else exists */
    player_start_thread(p);
    return p;
}

void player_destroy(ElyPlayer* p) {
    if (!p) return;
    /* Must stop and join the pump thread before touching anything it
     * might still be using, or this is a use-after-free race against the
     * thread's own in-flight settle/pump call. */
    player_stop_thread(p);
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
        audio_out_set_volume(p->audio_out,
                             p->pub_volume.load(std::memory_order_relaxed));
        /* A reset mid-play (seek, restart) re-opens the sink with its
         * clock stopped; a PLAYING player needs it running again or the
         * playhead freezes and end-of-media becomes unreachable. */
        if (p->state == ELY_STATE_PLAYING)
            audio_out_resume(p->audio_out);
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

        int got_kind = 0;
        size_t out_size = 0;
        double out_pts = 0.0, out_duration = 0.0;
        int out_keyframe = 0;
        if (!mp4_next_sample(p->demux, &got_kind,
                             p->demux_buf.data(), p->demux_buf.size(),
                             &out_size, &out_pts, &out_duration,
                             &out_keyframe)) {
            p->demux_eof = 1;
            break;
        }
        Packet pkt;
        memset(&pkt, 0, sizeof(pkt));
        pkt.size = out_size;
        pkt.data = (unsigned char*)malloc(out_size ? out_size : 1);
        if (!pkt.data) break;
        memcpy(pkt.data, p->demux_buf.data(), out_size);
        pkt.pts = out_pts;
        pkt.duration = out_duration;
        pkt.stream_kind = got_kind;
        pkt.keyframe = out_keyframe;
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
 * time, bounded per call so a single call cannot run forever. Called only
 * from the pump thread now (via player_settle(), from player_thread_main),
 * never from a getter - see player.h's own note on why that distinction
 * exists. Known transitional limitation, unchanged from before this
 * threading fix and still accepted deliberately: when decode runs slower
 * than real time (sanitizer builds; later, real codecs on slow machines),
 * a single pump call can spend significant wall time catching up to a
 * clock that keeps advancing. That wall time used to block whichever
 * caller happened to trigger it; now it only ever blocks the pump thread
 * itself, whose only job is exactly this, and other callers proceed as
 * soon as they can acquire ctl_mu after the current step. The threaded
 * demux/decode/render split described in the wider engine rewrite plan
 * retires this pump entirely; until then, tests pace their fixtures to
 * the build. Once the clock reaches the duration the horizon opens to
 * drain the tail, and if one call cannot finish it, the next one does. */
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
            /* Tri-state: a real decoder can accept a packet and produce no
             * frame yet (encoder lookahead), which is not a failure and
             * must not advance audio_failures. Only -1 (rejected packet)
             * does. */
            int rc = aac_decode_packet(p->audio_dec, &pkt, &pcm);
            if (rc > 0) {
                audio_out_write(p->audio_out, &pcm);
                p->audio_failures = 0;
            } else if (rc < 0 && ++p->audio_failures > 8) {
                packet_dispose(&pkt);
                return -1;
            }
            packet_dispose(&pkt);
            did = 1;
            work++;
        } else if (p->demux_eof && p->audio_q->count == 0 &&
                  p->demux->has_audio && !p->audio_eof) {
            /* Demux is done and nothing is queued, but the decoder may
             * still be holding buffered frames (B-frame-style reordering,
             * or an encoder's final lookahead window); drain it one frame
             * per pump call before declaring the audio track finished. */
            PcmFrame pcm;
            int rc = aac_decode_packet(p->audio_dec, nullptr, &pcm);
            if (rc > 0) {
                audio_out_write(p->audio_out, &pcm);
            } else {
                p->audio_eof = 1;
            }
            did = 1;
            work++;
        }
        if (p->video_q->count > 0 &&
            p->video_q->items[p->video_q->head].pts <= horizon) {
            Packet pkt;
            queue_pop(p->video_q, &pkt);
            VideoFrame frame;
            int rc = h264_decode_packet(p->video_dec, &pkt, &frame);
            if (rc > 0) {
                video_out_present(p->video_out, &frame);
                p->video_failures = 0;
            } else if (rc < 0 && ++p->video_failures > 8) {
                packet_dispose(&pkt);
                return -1;
            }
            packet_dispose(&pkt);
            did = 1;
            work++;
        } else if (p->demux_eof && p->video_q->count == 0 &&
                  p->demux->has_video && !p->video_eof) {
            VideoFrame frame;
            int rc = h264_decode_packet(p->video_dec, nullptr, &frame);
            if (rc > 0) {
                video_out_present(p->video_out, &frame);
            } else {
                p->video_eof = 1;
            }
            did = 1;
            work++;
        }
        if (!did) {
            if (p->demux_eof) {
                if (p->audio_q->count == 0 && !p->demux->has_audio)
                    p->audio_eof = 1;
                if (p->video_q->count == 0 && !p->demux->has_video)
                    p->video_eof = 1;
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
    /* Audio is the master whenever the software (later: device) backend
     * exposes a non-negative media-time clock; video-only media falls back
     * to the playback clock. The end gate tolerates the universal case of
     * an audio track ending slightly before the container duration: with
     * the pipeline fully drained, a playhead within 100ms of the duration
     * IS the natural end, or ENDED could never fire under audio master. */
    double device = audio_out_position(p->audio_out);
    double wall = clock_position(p->clock, p->info.duration, 0);
    double pos = (p->demux->has_audio && device >= 0.0) ? device : wall;
    /* Both clocks must agree the media is over. The device playhead alone
     * is not enough: a seek near the end drains the whole tail into the
     * sink instantly, and the wall time the pump itself spends decoding
     * would count against that queued audio, ending playback early. The
     * playback clock measures pure elapsed time since the seek and gets
     * NO tolerance: it can and must reach the full duration, so ENDED
     * cannot arrive before the media's final moments have actually been
     * lived through. The 0.1s tolerance applies only to the device
     * playhead, whose audio track legitimately ends slightly before the
     * container duration in nearly every real file. */
    if (p->info.duration > 0.0 && pos >= p->info.duration - 0.1 &&
        wall >= p->info.duration &&
        player_pipeline_drained(p)) {
        clock_pause(p->clock);
        clock_seek(p->clock, p->info.duration);
        p->state = ELY_STATE_ENDED;
    }
}
