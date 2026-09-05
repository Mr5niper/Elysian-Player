#pragma once
#include <stddef.h>
#include <vector>

/* The one shared definition of the pipeline's data types. The Part C plan
 * defined Packet in three headers at once, which cannot compile; everything
 * includes this instead. */

struct Packet {
    /* Owning, move-friendly buffer, replacing a previous raw malloc'd
     * unsigned char* with manual free() lifetime management. That
     * required every caller to remember to free it exactly once
     * (packet_dispose existed specifically to centralize that), and
     * offered no way to hand a packet to a queue or a decoder without an
     * explicit malloc+memcpy at each step. A std::vector needs none of
     * that: default construction, destruction, and moves are all
     * automatic and correct for free, and every remaining copy in this
     * pipeline (still one demux-to-packet copy in player_fill_queues,
     * still one packet-to-AVPacket copy in the decoders - see the
     * comments at each of those sites for why those two specifically are
     * not eliminated here) is now an explicit, visible std::vector copy
     * or assign call rather than a raw memcpy that could as easily have
     * been a use-after-free or a double-free with the old ownership
     * model. */
    std::vector<unsigned char> data;
    double pts = 0.0;
    double duration = 0.0;
    int stream_kind = 0;    /* 1 audio, 2 video, matching ElyMediaKind */
    int keyframe = 0;
};

struct PcmFrame {
    std::vector<float> samples;   /* interleaved */
    size_t frames = 0;
    int channels = 0;
    int sample_rate = 0;
    double pts = 0.0;
};

struct VideoFrame {
    /* BGRA, not RGBA: matches Win32's 32bpp DIB byte order directly, so
     * video_out's StretchDIBits path needs no channel swap per frame. */
    std::vector<unsigned char> pixels;
    int width = 0;
    int height = 0;
    int stride = 0;
    double pts = 0.0;
    int keyframe = 0;
};
