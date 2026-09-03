#pragma once
#include "frame.h"

/* AAC access units in, PCM out, behind the unchanged four-function seam.
 *
 * Part E: real spectral decode via FFmpeg's libavcodec AAC decoder, in
 * place of the structure-driven synthetic PCM this replaces. ctx/frame are
 * opaque (void*) so this header stays free of libav* include paths; only
 * aac_decode.cpp needs them.
 *
 * decode_packet's return value is tri-state, not boolean, because a real
 * decoder can legitimately accept a packet and produce no frame yet (an
 * encoder's lookahead/buffering delay): 1 a frame is ready in *out, 0 the
 * packet was consumed but no frame is ready yet (not a failure), -1 the
 * packet was rejected as bad data. player.cpp's failure counter only
 * advances on -1; a run of legitimate 0s must never trip it. Passing
 * pkt == nullptr signals end of stream: the decoder drains whatever it is
 * still holding, one frame per call, returning 0 once nothing is left. */
struct AacDecoder {
    int ready = 0;
    int sample_rate = 0;
    int channels = 0;
    void* ctx = nullptr;     /* AVCodecContext* */
    void* frame = nullptr;   /* AVFrame*, reused across calls */
    void* swr = nullptr;     /* SwrContext*, converts to interleaved float */
    /* Audio decode order equals presentation order (no B-frame-style
     * reordering), so the pts of the most recently sent packet is a
     * reliable stand-in for the pts of whatever frame comes out next. */
    double last_sent_pts = 0.0;
};

int aac_init(AacDecoder* d, const unsigned char* asc, size_t asc_size,
             int sample_rate, int channels);
void aac_flush(AacDecoder* d);
int aac_decode_packet(AacDecoder* d, const Packet* pkt, PcmFrame* out);
void aac_free(AacDecoder* d);
