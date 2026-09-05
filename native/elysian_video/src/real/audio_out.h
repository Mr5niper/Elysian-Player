#pragma once
#include "frame.h"

#include <atomic>

/* miniaudio is a single-header vendored library (native/elysian_video/
 * third_party/miniaudio.h). Declarations only are needed here; the
 * implementation is compiled once, in audio_out.cpp. AudioOut embeds
 * ma_device and ma_pcm_rb by value, so this header needs miniaudio's real
 * struct layouts, not just forward declarations - callers everywhere else
 * (player.h, abi_exports.cpp) only ever hold AudioOut behind a pointer and
 * never touch a field directly except the test-visibility counters below,
 * so this is the only file, along with audio_out.cpp, that needs to
 * change to give video files real audio output. */
#include "../../third_party/miniaudio.h"

/* Real OS-device-backed audio sink, replacing the former software-clocked
 * placeholder. A single-producer/single-consumer PCM ring buffer
 * (ma_pcm_rb, safe for exactly this usage pattern with no extra locking)
 * sits between the pump thread, which writes decoded PCM via
 * audio_out_write(), and miniaudio's own device callback thread, which
 * reads from it to feed the real audio device. frames_consumed is
 * incremented only by that callback, by the full frame count requested on
 * every invocation (including any silently zero-filled underrun frames),
 * so it tracks genuine elapsed hardware time exactly the way a real
 * device's own clock would - audio_out_position() derives the playback
 * clock directly from it, not from a software estimate.
 *
 * Failure to open a real device degrades gracefully rather than blocking
 * playback: device_ready stays false, writes become safe no-ops, and
 * position stays permanently unseeded (-1.0), which player_settle()
 * already treats as "no audio clock available" and falls back to the
 * playback clock for video-only-style timing - the same resilience this
 * engine already gives a video with no audio track at all. A machine with
 * no working audio device can still play video. */
struct AudioOut {
    float volume = 1.0f;
    int open = 0;
    int paused = 0;
    int sample_rate = 0;
    int channels = 0;

    /* test visibility only, matches the field name/semantics the existing
     * test suite already asserts on directly */
    size_t writes = 0;

    ma_device device{};
    ma_pcm_rb rb{};
    bool device_ready = false;   /* ma_device_init succeeded */
    bool rb_ready = false;       /* ma_pcm_rb_init succeeded */

    /* Written only by the device callback thread; read only from the pump
     * thread via audio_out_position(). Single writer, single reader. */
    std::atomic<unsigned long long> frames_consumed{0};
    /* Read by the device callback thread; written by audio_out_set_volume
     * from the pump thread. Applied as gain during the callback, the
     * first time volume has ever actually affected the audio signal
     * rather than being a stored-but-unused value. */
    std::atomic<float> live_volume{1.0f};

    double media_time_base = 0.0;   /* seeded from the first written frame's pts */
    bool seeded = false;
};

int audio_out_open(AudioOut* a, int sample_rate, int channels);
/* Test-only seam: identical to audio_out_open() but takes an explicit
 * miniaudio context, so a test can force a specific backend (e.g. the
 * null backend, for a sandbox with no real audio subsystem) without
 * touching the real open path at all. Production code always calls
 * audio_out_open(), which passes nullptr (default backend detection). */
int audio_out_open_ex(AudioOut* a, int sample_rate, int channels,
                      ma_context* ctx);
void audio_out_close(AudioOut* a);
void audio_out_pause(AudioOut* a);
void audio_out_resume(AudioOut* a);
void audio_out_flush(AudioOut* a);
int audio_out_write(AudioOut* a, const PcmFrame* frame);
double audio_out_position(AudioOut* a);   /* >= 0 means: real device clock available */
void audio_out_set_volume(AudioOut* a, float v);
