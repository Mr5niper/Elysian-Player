#pragma once
/* Milestone 2 seam: PCM sink (WASAPI on Windows). Volume is stored now so
 * the ABI's volume semantics hold before a device exists. */
struct AudioOut { float volume; };
int audio_out_open(AudioOut* a);
void audio_out_close(AudioOut* a);
void audio_out_set_volume(AudioOut* a, float v);
