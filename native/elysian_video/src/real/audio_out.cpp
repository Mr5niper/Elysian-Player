#include "audio_out.h"

int audio_out_open(AudioOut* a, int sample_rate, int channels) {
    if (!a || sample_rate <= 0 || channels <= 0) return 0;
    a->open = 1;
    a->paused = 0;
    a->sample_rate = sample_rate;
    a->channels = channels;
    a->written_seconds = 0.0;
    a->writes = 0;
    return 1;
}

void audio_out_close(AudioOut* a) {
    if (!a) return;
    a->open = 0;
    a->paused = 0;
    a->sample_rate = 0;
    a->channels = 0;
    a->written_seconds = 0.0;
    a->writes = 0;
}

void audio_out_pause(AudioOut* a) { if (a) a->paused = 1; }
void audio_out_resume(AudioOut* a) { if (a) a->paused = 0; }

void audio_out_flush(AudioOut* a) {
    if (!a) return;
    a->written_seconds = 0.0;   /* pending accounting only; not a clock */
}

int audio_out_write(AudioOut* a, const PcmFrame* frame) {
    if (!a || !a->open || !frame || frame->sample_rate <= 0) return 0;
    a->written_seconds += (double)frame->frames / (double)frame->sample_rate;
    a->writes++;
    return 1;
}

double audio_out_position(AudioOut* a) {
    (void)a;
    return -1.0;   /* no device clock until a real backend exists */
}

void audio_out_set_volume(AudioOut* a, float v) {
    if (!a) return;
    if (v < 0.0f) v = 0.0f;
    if (v > 1.0f) v = 1.0f;
    a->volume = v;
}
