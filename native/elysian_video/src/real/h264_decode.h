#pragma once
#include "frame.h"

/* H.264 access units in, RGBA-ready frames out, behind the unchanged
 * four-function seam.
 *
 * Part E: real macroblock reconstruction via FFmpeg's libavcodec H.264
 * decoder, in place of the slice-structure-driven synthetic frames this
 * replaces. ctx/frame/sws are opaque (void*) so this header stays free of
 * libav* include paths; only h264_decode.cpp needs them.
 *
 * decode_packet's return value is tri-state for the same reason as the
 * AAC seam: 1 a frame is ready, 0 accepted with no frame yet (B-frame
 * reordering delay; not a failure), -1 the packet was rejected. pkt ==
 * nullptr drains buffered frames at end of stream.
 *
 * VideoFrame.pixels comes out BGRA, not RGBA: video_out's Win32 paint path
 * hands this buffer straight to StretchDIBits, whose 32bpp DIB byte order
 * is blue-first, so converting here avoids a channel swap on every
 * presented frame. See frame.h. */
struct H264Decoder {
    int ready = 0;
    int width = 0;
    int height = 0;
    void* ctx = nullptr;   /* AVCodecContext* */
    void* frame = nullptr; /* AVFrame*, reused across calls */
    void* sws = nullptr;   /* SwsContext*, converts to BGRA */
    /* Falls back to the sending packet's own pts on the rare frame whose
     * best_effort_timestamp comes back unset (seen right at a flush/seek
     * boundary); kept current on every send. */
    double last_sent_pts = 0.0;
};

int h264_init(H264Decoder* d, const unsigned char* avcc, size_t avcc_size,
              int width, int height);
void h264_flush(H264Decoder* d);
int h264_decode_packet(H264Decoder* d, const Packet* pkt, VideoFrame* out);
void h264_free(H264Decoder* d);
