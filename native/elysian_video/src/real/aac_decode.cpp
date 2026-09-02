#include "aac_decode.h"
#include "bitreader.h"

static const int kAacRates[16] = {
    96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050,
    16000, 12000, 11025, 8000, 7350, 0, 0, 0
};

/* Parse the AudioSpecificConfig (ISO 14496-3): object type (5 bits, 31 +
 * 6-bit escape), sampling frequency index (4 bits, 15 = 24-bit explicit
 * rate), channel configuration (4 bits). Trailing extension bytes (sync
 * extension and friends) are legal and ignored. */
static int parse_asc(AacDecoder* d, const unsigned char* asc, size_t size) {
    BitReader br;
    br_init(&br, asc, size);
    int object_type = (int)br_read(&br, 5);
    if (object_type == 31) object_type = 32 + (int)br_read(&br, 6);
    int rate_index = (int)br_read(&br, 4);
    int rate = rate_index == 15 ? (int)br_read(&br, 24)
             : rate_index < 13 ? kAacRates[rate_index] : 0;
    int channel_config = (int)br_read(&br, 4);
    if (!br_ok(&br) || rate <= 0) return 0;
    d->asc_object_type = object_type;
    d->asc_rate_index = rate_index;
    d->asc_rate = rate;
    d->asc_channel_config = channel_config;
    return 1;
}

int aac_init(AacDecoder* d, const unsigned char* asc, size_t asc_size,
             int sample_rate, int channels) {
    if (!d || !asc || asc_size == 0 || sample_rate <= 0 || channels <= 0)
        return 0;
    if (!parse_asc(d, asc, asc_size))
        return 0;
    /* Owned-decoder v1 scope: AAC-LC, mono or stereo, and the container's
     * story must agree with the codec config. */
    if (d->asc_object_type != 2) return 0;
    if (d->asc_channel_config < 1 || d->asc_channel_config > 2) return 0;
    if (d->asc_rate != sample_rate) return 0;
    if (d->asc_channel_config != channels) return 0;

    d->codec_config.assign(asc, asc + asc_size);
    d->sample_rate = sample_rate;
    d->channels = channels;
    d->overlap[0].assign(1024, 0.0f);
    d->overlap[1].assign(1024, 0.0f);
    d->window_shape[0] = d->window_shape[1] = 0;
    d->ready = 1;
    return 1;
}

void aac_flush(AacDecoder* d) {
    if (!d) return;
    /* Seek discontinuity: drop overlap-add history so the first frame
     * after a seek cannot ring with pre-seek audio. */
    d->overlap[0].assign(d->overlap[0].size(), 0.0f);
    d->overlap[1].assign(d->overlap[1].size(), 0.0f);
}

int aac_decode_packet(AacDecoder* d, const Packet* pkt, PcmFrame* out) {
    if (!d || !d->ready || !pkt || !out) return 0;
    /* Real preconditions, kept forever: an access unit with no bytes is
     * malformed and must fail, which is also what lets the pipeline's
     * failure-escalation path be tested honestly. */
    if (!pkt->data || pkt->size == 0) return 0;

    /* Spectral decode lands here next; until then, correctly-shaped
     * silence keeps the pipeline running end to end. */
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
    d->overlap[0].clear();
    d->overlap[1].clear();
}
