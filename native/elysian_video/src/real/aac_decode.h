#pragma once
/* Milestone 2 seam: AAC access units in, PCM out. The demuxer already
 * hands over the AudioSpecificConfig; this is where the decode decision
 * from CONTRACT.md (hand-rolled vs platform decoder) gets implemented. */
struct AacDecoder { int ready; };
int aac_init(AacDecoder* d);
void aac_free(AacDecoder* d);
