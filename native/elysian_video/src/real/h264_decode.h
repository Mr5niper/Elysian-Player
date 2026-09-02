#pragma once
/* Milestone 2 seam: H.264 access units in, frames out. The demuxer already
 * hands over the avcC record; the same decode decision applies here. */
struct H264Decoder { int ready; };
int h264_init(H264Decoder* d);
void h264_free(H264Decoder* d);
