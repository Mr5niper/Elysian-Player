#include "audio_out.h"

#ifdef _WIN32
#include <windows.h>
static double now_seconds(void) {
    static LARGE_INTEGER freq;
    LARGE_INTEGER c;
    if (!freq.QuadPart) QueryPerformanceFrequency(&freq);
    QueryPerformanceCounter(&c);
    return (double)c.QuadPart / (double)freq.QuadPart;
}
#else
#include <time.h>
static double now_seconds(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}
#endif

static double queued_seconds(const AudioOut* a) {
    if (!a || a->sample_rate <= 0) return 0.0;
    if (a->queued_frames <= a->consumed_frames) return 0.0;
    return (double)(a->queued_frames - a->consumed_frames) /
           (double)a->sample_rate;
}

static void settle_clock(AudioOut* a) {
    if (!a || !a->open || !a->running || a->paused) return;
    double now = now_seconds();
    double elapsed = now - a->wall_started;
    if (elapsed < 0.0) elapsed = 0.0;

    double max_advance = queued_seconds(a);
    double advance = elapsed;
    if (advance > max_advance) advance = max_advance;

    a->playhead_seconds += advance;
    size_t frames = (size_t)(advance * (double)a->sample_rate);
    a->consumed_frames += frames;
    if (a->consumed_frames > a->queued_frames)
        a->consumed_frames = a->queued_frames;

    a->wall_started = now;
}

int audio_out_open(AudioOut* a, int sample_rate, int channels) {
    if (!a || sample_rate <= 0 || channels <= 0) return 0;
    a->open = 1;
    a->paused = 0;
    a->running = 0;
    a->sample_rate = sample_rate;
    a->channels = channels;
    a->queued_frames = 0;
    a->consumed_frames = 0;
    a->playhead_seconds = 0.0;
    a->wall_started = now_seconds();
    a->seeded = 0;
    a->writes = 0;
    return 1;
}

void audio_out_close(AudioOut* a) {
    if (!a) return;
    a->open = 0;
    a->paused = 0;
    a->running = 0;
    a->sample_rate = 0;
    a->channels = 0;
    a->queued_frames = 0;
    a->consumed_frames = 0;
    a->playhead_seconds = 0.0;
    a->wall_started = 0.0;
    a->seeded = 0;
    a->writes = 0;
}

void audio_out_pause(AudioOut* a) {
    if (!a) return;
    settle_clock(a);
    a->paused = 1;
    a->running = 0;
}

void audio_out_resume(AudioOut* a) {
    if (!a || !a->open) return;
    a->paused = 0;
    a->running = 1;
    a->wall_started = now_seconds();
}

void audio_out_flush(AudioOut* a) {
    if (!a) return;
    a->queued_frames = 0;
    a->consumed_frames = 0;
    a->playhead_seconds = 0.0;
    a->wall_started = now_seconds();
    a->running = 0;
    a->paused = 0;
    a->seeded = 0;
}

int audio_out_write(AudioOut* a, const PcmFrame* frame) {
    if (!a || !a->open || !frame || frame->sample_rate <= 0) return 0;
    if (frame->sample_rate != a->sample_rate || frame->channels != a->channels)
        return 0;
    if (!a->seeded) {
        /* Media time starts where the stream does: first write after an
         * open or flush seeds the playhead with that frame's PTS, so a
         * post-seek clock reads 5.0, not 0.0. */
        a->playhead_seconds = frame->pts;
        a->seeded = 1;
    }
    a->queued_frames += frame->frames;
    a->writes++;
    return 1;
}

double audio_out_position(AudioOut* a) {
    if (!a || !a->open || !a->seeded) return -1.0;
    settle_clock(a);
    return a->playhead_seconds;
}

void audio_out_set_volume(AudioOut* a, float v) {
    if (!a) return;
    if (v < 0.0f) v = 0.0f;
    if (v > 1.0f) v = 1.0f;
    a->volume = v;
}
