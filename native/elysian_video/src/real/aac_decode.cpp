#include "aac_decode.h"

int aac_init(AacDecoder* d, const unsigned char* asc, size_t asc_size,
             int sample_rate, int channels) {
    if (!d || !asc || asc_size == 0 || sample_rate <= 0 || channels <= 0)
        return 0;
    d->codec_config.assign(asc, asc + asc_size);
    d->sample_rate = sample_rate;
    d->channels = channels;
    d->ready = 1;
    return 1;
}

void aac_flush(AacDecoder* d) {
    (void)d;    /* stateless while synthetic; real decode resets here */
}

int aac_decode_packet(AacDecoder* d, const Packet* pkt, PcmFrame* out) {
    if (!d || !d->ready || !pkt || !out) return 0;
    /* Synthetic stepping stone: silence with one access unit's duration,
     * so queue flow, output accounting and drain rules are exercised for
     * real. Actual decode replaces only the body of this function. */
    const size_t frames = 1024;
    out->channels = d->channels;
    out->sample_rate = d->sample_rate;
    out->frames = frames;
    out->pts = pkt->pts;
    out->samples.assign(frames * (size_t)d->channels, 0.0f);
    return 1;
}

void aac_free(AacDecoder* d) {
    if (!d) return;
    d->ready = 0;
    d->sample_rate = 0;
    d->channels = 0;
    d->codec_config.clear();
}
