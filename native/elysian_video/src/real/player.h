#pragma once
#include "../../include/elysian_video.h"

#include <string>
#include <vector>

struct Mp4Demux;
struct AacDecoder;
struct H264Decoder;
struct AudioOut;
struct VideoOut;
struct PlaybackClock;
struct PacketQueue;

struct ElyPlayer {
    int state;                  /* ElyState */
    std::wstring path;
    ElyMediaInfo info;
    float volume;
    void* hwnd;
    int target_w;
    int target_h;
    wchar_t last_error[256];

    PlaybackClock* clock;
    Mp4Demux* demux;
    AacDecoder* audio_dec;
    H264Decoder* video_dec;
    AudioOut* audio_out;
    VideoOut* video_out;

    /* ---- Part C pipeline state ------------------------------------------ */
    PacketQueue* audio_q;
    PacketQueue* video_q;
    std::vector<unsigned char> demux_buf;
    int demux_eof;
    int audio_ready;
    int video_ready;
    int audio_eof;
    int video_eof;
};

ElyPlayer* player_create(void);
void player_destroy(ElyPlayer* p);
void player_set_error(ElyPlayer* p, const wchar_t* msg);

/* Lazily promote PLAYING to ENDED. Since Part C, ENDED requires the clock
 * at the duration AND the pipeline drained (demux EOF, queues empty,
 * decoders flushed through), matching the contract's drained-EOF rule.
 * Every ABI getter and command funnels through this. */
void player_settle(ElyPlayer* p);

/* Part C internals: not ABI, used by abi_exports.cpp and tests only. */
int player_prepare_pipeline(ElyPlayer* p);
void player_reset_pipeline(ElyPlayer* p);
int player_fill_queues(ElyPlayer* p, int max_packets);
int player_pump(ElyPlayer* p);
int player_pipeline_drained(const ElyPlayer* p);
