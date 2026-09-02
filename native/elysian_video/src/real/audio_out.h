#pragma once
#include "frame.h"

/* Software-clocked PCM sink for Part C completion.
 *
 * No longer a written-frames counter and not yet the OS device backend: a
 * playhead that advances with monotonic wall time while running, clamps to
 * queued audio, and freezes on pause. The playhead is seeded from the PTS
 * of the first frame written after open/flush, so it reports MEDIA time,
 * exactly as a real device clock would; a stream-relative clock would make
 * end-of-media unreachable after any seek. */
struct AudioOut {
    float volume = 1.0f;
    int open = 0;
    int paused = 0;
    int sample_rate = 0;
    int channels = 0;

    /* queued audio, in frames */
    size_t queued_frames = 0;
    size_t consumed_frames = 0;

    /* software playback clock, media time */
    double playhead_seconds = 0.0;
    double wall_started = 0.0;
    int running = 0;
    int seeded = 0;

    /* test visibility */
    size_t writes = 0;
};

int audio_out_open(AudioOut* a, int sample_rate, int channels);
void audio_out_close(AudioOut* a);
void audio_out_pause(AudioOut* a);
void audio_out_resume(AudioOut* a);
void audio_out_flush(AudioOut* a);
int audio_out_write(AudioOut* a, const PcmFrame* frame);
double audio_out_position(AudioOut* a);   /* >= 0 means: real software clock */
void audio_out_set_volume(AudioOut* a, float v);
