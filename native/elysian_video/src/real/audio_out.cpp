#include "audio_out.h"
int audio_out_open(AudioOut* a) { a->volume = 1.0f; return 1; }
void audio_out_close(AudioOut* a) { a->volume = 0.0f; }
void audio_out_set_volume(AudioOut* a, float v) { a->volume = v; }
