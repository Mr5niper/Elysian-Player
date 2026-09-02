#pragma once
#include "../../include/elysian_video.h"

#include <string>

struct Mp4Demux;
struct AacDecoder;
struct H264Decoder;
struct AudioOut;
struct VideoOut;
struct PlaybackClock;

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
};

ElyPlayer* player_create(void);
void player_destroy(ElyPlayer* p);
void player_set_error(ElyPlayer* p, const wchar_t* msg);

/* Lazily promote PLAYING to ENDED when the clock passes the duration.
 * Every ABI getter and command funnels through this, matching the contract:
 * ENDED must be observable through any getter, not only get_position. */
void player_settle(ElyPlayer* p);
