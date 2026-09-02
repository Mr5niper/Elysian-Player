#include "player.h"
#include "clock.h"
#include "mp4_demux.h"
#include "aac_decode.h"
#include "h264_decode.h"
#include "audio_out.h"
#include "video_out.h"

#include <string.h>
#include <wchar.h>

static void clear_info(ElyMediaInfo* info) {
    memset(info, 0, sizeof(*info));
    info->struct_size = (int)sizeof(ElyMediaInfo);
    info->kind = ELY_MEDIA_UNKNOWN;
}

void player_set_error(ElyPlayer* p, const wchar_t* msg) {
    if (!p) return;
    wcsncpy(p->last_error, msg, 255);
    p->last_error[255] = 0;
}

ElyPlayer* player_create(void) {
    ElyPlayer* p = new (std::nothrow) ElyPlayer();
    if (!p) return NULL;
    p->state = ELY_STATE_EMPTY;
    p->volume = 1.0f;
    p->hwnd = NULL;
    p->target_w = p->target_h = 0;
    p->last_error[0] = 0;
    clear_info(&p->info);

    p->clock = new (std::nothrow) PlaybackClock();
    p->demux = new (std::nothrow) Mp4Demux();
    p->audio_dec = new (std::nothrow) AacDecoder();
    p->video_dec = new (std::nothrow) H264Decoder();
    p->audio_out = new (std::nothrow) AudioOut();
    p->video_out = new (std::nothrow) VideoOut();
    if (!p->clock || !p->demux || !p->audio_dec || !p->video_dec ||
        !p->audio_out || !p->video_out) {
        player_destroy(p);
        return NULL;
    }
    memset(p->video_out, 0, sizeof(VideoOut));
    clock_reset(p->clock, 0.0);
    audio_out_open(p->audio_out);
    return p;
}

void player_destroy(ElyPlayer* p) {
    if (!p) return;
    if (p->demux) mp4_close(p->demux);
    if (p->audio_out) audio_out_close(p->audio_out);
    delete p->clock;
    delete p->demux;
    delete p->audio_dec;
    delete p->video_dec;
    delete p->audio_out;
    delete p->video_out;
    delete p;
}

void player_settle(ElyPlayer* p) {
    if (!p || p->state != ELY_STATE_PLAYING) return;
    double pos = clock_position(p->clock, p->info.duration, 0);
    if (p->info.duration > 0.0 && pos >= p->info.duration) {
        clock_pause(p->clock);
        clock_seek(p->clock, p->info.duration);
        p->state = ELY_STATE_ENDED;
    }
}
