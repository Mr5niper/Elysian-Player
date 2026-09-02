#pragma once
#include <stddef.h>
#include <vector>

/* The one shared definition of the pipeline's data types. The Part C plan
 * defined Packet in three headers at once, which cannot compile; everything
 * includes this instead. Ownership rule for Packet.data: allocated by the
 * demux pump in player.cpp, freed by whoever pops it from a queue, and
 * player_reset_pipeline frees anything still queued. */

struct Packet {
    unsigned char* data;
    size_t size;
    double pts;
    double duration;
    int stream_kind;    /* 1 audio, 2 video, matching ElyMediaKind */
    int keyframe;
};

struct PcmFrame {
    std::vector<float> samples;   /* interleaved */
    size_t frames = 0;
    int channels = 0;
    int sample_rate = 0;
    double pts = 0.0;
};

struct VideoFrame {
    std::vector<unsigned char> pixels;  /* RGBA */
    int width = 0;
    int height = 0;
    int stride = 0;
    double pts = 0.0;
    int keyframe = 0;
};
