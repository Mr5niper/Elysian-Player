#define MINIAUDIO_IMPLEMENTATION
#include "audio_out.h"

#include <stdlib.h>
#include <string.h>

/* Called on miniaudio's own internal audio thread, never on the pump
 * thread. Reads from the ring buffer (the single consumer) and applies
 * live_volume as gain; any shortfall (buffer underrun: not enough decoded
 * PCM available yet) is filled with silence rather than left as garbage
 * or a short device buffer, and frames_consumed still advances by the
 * full frameCount requested either way, since that count is what the
 * audio hardware actually clocked through in real time regardless of
 * whether this engine had data ready for it. */
static void data_callback(ma_device* pDevice, void* pOutput, const void* pInput,
                          ma_uint32 frameCount) {
    (void)pInput;
    AudioOut* a = (AudioOut*)pDevice->pUserData;
    float* out = (float*)pOutput;
    ma_uint32 channels = (ma_uint32)a->channels;
    ma_uint32 framesWritten = 0;

    if (a->rb_ready) {
        float vol = a->live_volume.load(std::memory_order_relaxed);
        while (framesWritten < frameCount) {
            ma_uint32 framesToRead = frameCount - framesWritten;
            void* pReadBuf = nullptr;
            ma_result rc = ma_pcm_rb_acquire_read(&a->rb, &framesToRead, &pReadBuf);
            if (rc != MA_SUCCESS || framesToRead == 0)
                break;
            const float* src = (const float*)pReadBuf;
            float* dst = out + (size_t)framesWritten * channels;
            size_t n = (size_t)framesToRead * channels;
            for (size_t i = 0; i < n; i++)
                dst[i] = src[i] * vol;
            ma_pcm_rb_commit_read(&a->rb, framesToRead);
            framesWritten += framesToRead;
        }
    }

    if (framesWritten < frameCount) {
        /* Underrun: silence, not garbage. Normal and expected right after
         * open/seek, before the pump thread has written anything yet. */
        float* dst = out + (size_t)framesWritten * channels;
        size_t remaining = (size_t)(frameCount - framesWritten) * channels;
        memset(dst, 0, remaining * sizeof(float));
    }

    a->frames_consumed.fetch_add(frameCount, std::memory_order_relaxed);
}

int audio_out_open(AudioOut* a, int sample_rate, int channels) {
    /* Test-only escape hatch, inert unless explicitly set: this sandbox
     * has no real audio subsystem at all, and redirecting ALSA's default
     * to its own null plugin (done only for local testing, never in a
     * real deployment) does not pace playback to real time the way a
     * genuine device does - it just accepts writes as fast as the CPU
     * can loop. miniaudio's own null backend does pace correctly (it
     * simulates real device timing internally), so tests force it via
     * this variable to get a trustworthy signal in an environment with
     * no real hardware, instead of nullptr's default-backend detection
     * picking up whatever degraded fallback the sandbox happens to
     * resolve to. Never set in a real build. */
    if (getenv("ELYSIAN_TEST_NULL_AUDIO_BACKEND")) {
        static ma_context s_test_ctx;
        static bool s_test_ctx_ready = false;
        if (!s_test_ctx_ready) {
            ma_backend backends[] = { ma_backend_null };
            s_test_ctx_ready =
                ma_context_init(backends, 1, nullptr, &s_test_ctx) == MA_SUCCESS;
        }
        if (s_test_ctx_ready)
            return audio_out_open_ex(a, sample_rate, channels, &s_test_ctx);
    }
    return audio_out_open_ex(a, sample_rate, channels, nullptr);
}

int audio_out_open_ex(AudioOut* a, int sample_rate, int channels,
                      ma_context* ctx) {
    if (!a || sample_rate <= 0 || channels <= 0) return 0;

    /* player_prepare_pipeline() calls this again every time
     * player_reset_pipeline() has run (every seek, stop, and restart),
     * since that resets audio_ready to 0. The old software clock treated
     * that as free (a few counters reset); tearing down and recreating a
     * real OS audio device on every seek is not free, and repeated seeks
     * in quick succession (exactly what the existing contract test does)
     * made a comfortably-timed test start missing its window purely from
     * that accumulated reinitialization cost - a real behavioral
     * difference from a real device, not a bug in the timing logic
     * itself. It would also be audibly wrong on real hardware: reopening
     * the device on every seek risks a click each time. If the format
     * has not actually changed, there is nothing to reinitialize -
     * audio_out_flush() (called separately, before this, by
     * player_reset_pipeline()) already reset the ring buffer and the
     * seeded/position state; only a genuine format change (a different
     * file with a different sample rate or channel count) needs a full
     * teardown and recreation of the real device. */
    if (a->device_ready && a->rb_ready &&
        a->sample_rate == sample_rate && a->channels == channels) {
        a->open = 1;
        a->paused = 0;
        a->writes = 0;
        return 1;
    }

    /* Tear down any existing device/ring buffer before reinitializing.
     * Reusing the same ma_device/ma_pcm_rb structures without
     * uninitializing them first is undefined behavior against a real OS
     * resource - a real bug found while testing this against the
     * existing contract suite's seek/restart cases, not a sandbox
     * artifact. */
    if (a->device_ready) {
        ma_device_uninit(&a->device);
        a->device_ready = false;
    }
    if (a->rb_ready) {
        ma_pcm_rb_uninit(&a->rb);
        a->rb_ready = false;
    }

    a->open = 1;
    a->paused = 0;
    a->sample_rate = sample_rate;
    a->channels = channels;
    a->writes = 0;
    a->seeded = false;
    a->media_time_base = 0.0;
    a->frames_consumed.store(0, std::memory_order_relaxed);
    a->live_volume.store(a->volume, std::memory_order_relaxed);
    a->device_ready = false;
    a->rb_ready = false;

    /* One second of buffering: generous headroom against the pump
     * thread's own write cadence without meaningfully affecting latency,
     * since the callback only ever drains from it, never blocks on it. */
    ma_uint32 rb_capacity = (ma_uint32)sample_rate;
    if (ma_pcm_rb_init(ma_format_f32, (ma_uint32)channels, rb_capacity,
                       nullptr, nullptr, &a->rb) == MA_SUCCESS) {
        a->rb_ready = true;
    }

    ma_device_config config = ma_device_config_init(ma_device_type_playback);
    config.playback.format = ma_format_f32;
    config.playback.channels = (ma_uint32)channels;
    config.sampleRate = (ma_uint32)sample_rate;
    config.dataCallback = data_callback;
    config.pUserData = a;

    /* ctx == nullptr: default backend auto-detection (WASAPI on Windows
     * in production). A real device failing to open degrades gracefully
     * rather than blocking playback - see the module comment in
     * audio_out.h for why that matters and what it falls back to.
     * audio_out_open_ex exists purely so tests can force a specific
     * backend (the null backend, for environments with no real audio
     * subsystem at all) without touching this logic at all. */
    if (a->rb_ready &&
        ma_device_init(ctx, &config, &a->device) == MA_SUCCESS) {
        a->device_ready = true;
    }

    return 1;   /* always succeeds at the ABI/pipeline level; see header */
}

void audio_out_close(AudioOut* a) {
    if (!a) return;
    if (a->device_ready) {
        ma_device_uninit(&a->device);
        a->device_ready = false;
    }
    if (a->rb_ready) {
        ma_pcm_rb_uninit(&a->rb);
        a->rb_ready = false;
    }
    a->open = 0;
    a->paused = 0;
    a->sample_rate = 0;
    a->channels = 0;
    a->writes = 0;
    a->seeded = false;
    a->media_time_base = 0.0;
    a->frames_consumed.store(0, std::memory_order_relaxed);
}

void audio_out_pause(AudioOut* a) {
    if (!a) return;
    if (a->device_ready)
        ma_device_stop(&a->device);   /* callback stops firing; clock freezes naturally */
    a->paused = 1;
}

void audio_out_resume(AudioOut* a) {
    if (!a || !a->open) return;
    a->paused = 0;
    if (a->device_ready)
        ma_device_start(&a->device);
}

void audio_out_flush(AudioOut* a) {
    if (!a) return;
    if (a->device_ready)
        ma_device_stop(&a->device);
    if (a->rb_ready)
        ma_pcm_rb_reset(&a->rb);
    a->frames_consumed.store(0, std::memory_order_relaxed);
    a->seeded = false;
    a->media_time_base = 0.0;
    a->paused = 0;
}

int audio_out_write(AudioOut* a, const PcmFrame* frame) {
    if (!a || !a->open || !frame || frame->sample_rate <= 0) return 0;
    if (frame->sample_rate != a->sample_rate || frame->channels != a->channels)
        return 0;

    if (!a->seeded) {
        /* Media time starts where the stream does: first write after an
         * open or flush seeds the clock with that frame's PTS, so a
         * post-seek clock reads 5.0, not 0.0 - unchanged contract from
         * the software-clock version. */
        a->media_time_base = frame->pts;
        a->seeded = true;
    }
    a->writes++;

    if (!a->rb_ready || frame->frames == 0)
        return 1;   /* no real device: accepted but has nowhere to go */

    size_t offset = 0;   /* in frames */
    while (offset < frame->frames) {
        ma_uint32 framesToWrite = (ma_uint32)(frame->frames - offset);
        void* pWriteBuf = nullptr;
        ma_result rc = ma_pcm_rb_acquire_write(&a->rb, &framesToWrite, &pWriteBuf);
        if (rc != MA_SUCCESS || framesToWrite == 0)
            break;   /* ring buffer full: drop the remainder rather than block */
        size_t n = (size_t)framesToWrite * (size_t)a->channels;
        memcpy(pWriteBuf,
              frame->samples.data() + offset * (size_t)a->channels,
              n * sizeof(float));
        ma_pcm_rb_commit_write(&a->rb, framesToWrite);
        offset += framesToWrite;
    }
    return 1;
}

double audio_out_position(AudioOut* a) {
    if (!a || !a->open || !a->seeded) return -1.0;
    unsigned long long consumed = a->frames_consumed.load(std::memory_order_relaxed);
    return a->media_time_base + (double)consumed / (double)a->sample_rate;
}

void audio_out_set_volume(AudioOut* a, float v) {
    if (!a) return;
    if (v < 0.0f) v = 0.0f;
    if (v > 1.0f) v = 1.0f;
    a->volume = v;
    a->live_volume.store(v, std::memory_order_relaxed);
}
