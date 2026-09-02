#pragma once
#include "frame.h"

/* First usable PCM sink: accepts writes, tracks accounting, honors volume
 * and pause. It deliberately has NO device clock yet: audio_out_position
 * returns a negative value meaning "no clock here", and the player falls
 * back to the playback clock. A written-frames counter is NOT a clock - it
 * advances at pump cadence, not wall time, and using it as master would
 * break every position semantic in the contract. When the WASAPI backend
 * lands, position() reports the real device clock and automatically becomes
 * master, which is what the contract means by audio-driven position. */
struct AudioOut {
    float volume = 1.0f;
    int open = 0;
    int paused = 0;
    int sample_rate = 0;
    int channels = 0;
    double written_seconds = 0.0;   /* accounting, not a clock */
    size_t writes = 0;
};

int audio_out_open(AudioOut* a, int sample_rate, int channels);
void audio_out_close(AudioOut* a);
void audio_out_pause(AudioOut* a);
void audio_out_resume(AudioOut* a);
void audio_out_flush(AudioOut* a);
int audio_out_write(AudioOut* a, const PcmFrame* frame);
double audio_out_position(AudioOut* a);   /* < 0 means: no device clock */
void audio_out_set_volume(AudioOut* a, float v);
