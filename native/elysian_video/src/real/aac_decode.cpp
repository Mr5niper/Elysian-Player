#include "aac_decode.h"
int aac_init(AacDecoder* d) { d->ready = 1; return 1; }
void aac_free(AacDecoder* d) { d->ready = 0; }
