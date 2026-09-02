#include "h264_decode.h"
int h264_init(H264Decoder* d) { d->ready = 1; return 1; }
void h264_free(H264Decoder* d) { d->ready = 0; }
