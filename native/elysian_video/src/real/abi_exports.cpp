#define ELY_VIDEO_EXPORTS
#include "../../include/elysian_video.h"
#include "player.h"
#include "clock.h"
#include "mp4_demux.h"
#include "audio_out.h"
#include "video_out.h"

#include <string.h>

static int fail(ElyPlayer* p, int code, const wchar_t* msg) {
    player_set_error(p, msg);
    return code;
}

extern "C" {

ELY_API int ely_abi_version(void) { return ELY_ABI_VERSION; }

ELY_API ElyPlayer* ely_create_player(void) { return player_create(); }

ELY_API void ely_destroy_player(ElyPlayer* p) { player_destroy(p); }

ELY_API int ely_load(ElyPlayer* p, const wchar_t* path) {
    if (!p) return ELY_ERR_BAD_ARG;
    if (!path || !path[0])
        return fail(p, ELY_ERR_BAD_ARG, L"load: empty path");

    /* Implicit unload of any previous media, per contract. */
    mp4_close(p->demux);
    p->path.clear();
    memset(&p->info, 0, sizeof(p->info));
    p->info.struct_size = (int)sizeof(ElyMediaInfo);

    int rc = mp4_open(p->demux, path);
    if (rc == ELY_ERR_NOT_FOUND)
        return fail(p, rc, L"load: file not found");
    if (rc == ELY_ERR_UNSUPPORTED)
        return fail(p, rc, L"load: not an MP4 family file");
    if (rc == ELY_ERR_BAD_CONTAINER)
        return fail(p, rc, L"load: damaged MP4 container");
    if (rc == ELY_ERR_BAD_STREAM)
        return fail(p, rc, L"load: no playable audio or video track");
    if (rc != ELY_OK)
        return fail(p, rc, L"load: failed");

    mp4_fill_info(p->demux, &p->info);
    p->info.struct_size = (int)sizeof(ElyMediaInfo);
    p->path = path;
    clock_reset(p->clock, 0.0);
    player_reset_pipeline(p);
    p->state = ELY_STATE_LOADED;
    /* Deliberately no error-buffer clear: success never clears last_error,
     * per the locked contract rule. */
    return ELY_OK;
}

ELY_API int ely_unload(ElyPlayer* p) {
    if (!p) return ELY_ERR_BAD_ARG;
    player_reset_pipeline(p);
    mp4_close(p->demux);
    p->path.clear();
    memset(&p->info, 0, sizeof(p->info));
    p->info.struct_size = (int)sizeof(ElyMediaInfo);
    clock_reset(p->clock, 0.0);
    p->state = ELY_STATE_EMPTY;
    return ELY_OK;
}

ELY_API int ely_play(ElyPlayer* p) {
    if (!p) return ELY_ERR_BAD_ARG;
    player_settle(p);
    switch (p->state) {
    case ELY_STATE_LOADED:
    case ELY_STATE_PAUSED:
        break;                              /* start / resume from current */
    case ELY_STATE_STOPPED:
    case ELY_STATE_ENDED:
        player_reset_pipeline(p);           /* restart from zero */
        mp4_seek(p->demux, 0.0);
        clock_seek(p->clock, 0.0);
        break;
    case ELY_STATE_PLAYING:
        return ELY_OK;                      /* idempotent, no rewind */
    default:
        return fail(p, ELY_ERR_BAD_STATE, L"play: nothing loaded");
    }
    if (!player_prepare_pipeline(p))
        return fail(p, ELY_ERR_DECODE, L"play: pipeline init failed");
    audio_out_resume(p->audio_out);
    clock_play(p->clock);
    p->state = ELY_STATE_PLAYING;
    return ELY_OK;
}

ELY_API int ely_pause(ElyPlayer* p) {
    if (!p) return ELY_ERR_BAD_ARG;
    player_settle(p);
    if (p->state != ELY_STATE_PLAYING)
        return fail(p, ELY_ERR_BAD_STATE, L"pause: not playing");
    clock_pause(p->clock);
    audio_out_pause(p->audio_out);
    p->state = ELY_STATE_PAUSED;
    return ELY_OK;
}

ELY_API int ely_resume(ElyPlayer* p) {
    if (!p) return ELY_ERR_BAD_ARG;
    if (p->state != ELY_STATE_PAUSED)
        return fail(p, ELY_ERR_BAD_STATE, L"resume: not paused");
    audio_out_resume(p->audio_out);
    clock_play(p->clock);
    p->state = ELY_STATE_PLAYING;
    return ELY_OK;
}

ELY_API int ely_stop(ElyPlayer* p) {
    if (!p) return ELY_ERR_BAD_ARG;
    if (p->state == ELY_STATE_EMPTY)
        return fail(p, ELY_ERR_BAD_STATE, L"stop: nothing loaded");
    player_reset_pipeline(p);
    mp4_seek(p->demux, 0.0);
    clock_reset(p->clock, 0.0);
    p->state = ELY_STATE_STOPPED;           /* media stays loaded */
    return ELY_OK;
}

ELY_API int ely_seek(ElyPlayer* p, double seconds) {
    if (!p) return ELY_ERR_BAD_ARG;
    player_settle(p);
    if (p->state == ELY_STATE_EMPTY)
        return fail(p, ELY_ERR_BAD_STATE, L"seek: nothing loaded");
    if (seconds < 0.0) seconds = 0.0;
    if (p->info.duration > 0.0 && seconds > p->info.duration)
        seconds = p->info.duration;
    player_reset_pipeline(p);               /* flush queues and decoders */
    if (!mp4_seek(p->demux, seconds))
        return fail(p, ELY_ERR_SEEK, L"seek: demuxer refused the target");
    clock_seek(p->clock, seconds);
    if (p->state == ELY_STATE_ENDED && seconds < p->info.duration)
        p->state = ELY_STATE_PAUSED;        /* never silently playing */
    return ELY_OK;
}

ELY_API int ely_set_volume(ElyPlayer* p, float volume) {
    if (!p) return ELY_ERR_BAD_ARG;
    if (volume < 0.0f) volume = 0.0f;
    if (volume > 1.0f) volume = 1.0f;
    p->volume = volume;
    audio_out_set_volume(p->audio_out, volume);
    return ELY_OK;
}

ELY_API float ely_get_volume(ElyPlayer* p) { return p ? p->volume : 0.0f; }

ELY_API double ely_get_position(ElyPlayer* p) {
    if (!p) return 0.0;
    player_settle(p);
    if (p->state == ELY_STATE_EMPTY || p->state == ELY_STATE_STOPPED)
        return 0.0;
    return clock_position(p->clock, p->info.duration, 1);
}

ELY_API double ely_get_duration(ElyPlayer* p) {
    if (!p || p->state == ELY_STATE_EMPTY) return 0.0;
    return p->info.duration;
}

ELY_API int ely_is_playing(ElyPlayer* p) {
    if (!p) return 0;
    player_settle(p);
    return p->state == ELY_STATE_PLAYING;
}

ELY_API int ely_is_paused(ElyPlayer* p) {
    if (!p) return 0;
    player_settle(p);
    return p->state == ELY_STATE_PAUSED;
}

ELY_API int ely_is_active(ElyPlayer* p) {
    if (!p) return 0;
    player_settle(p);
    return p->state == ELY_STATE_PLAYING || p->state == ELY_STATE_PAUSED;
}

ELY_API int ely_is_finished(ElyPlayer* p) {
    if (!p) return 0;
    player_settle(p);
    return p->state == ELY_STATE_ENDED;
}

ELY_API int ely_get_state(ElyPlayer* p) {
    if (!p) return ELY_STATE_ERROR;
    player_settle(p);
    return p->state;
}

ELY_API int ely_get_media_info(ElyPlayer* p, ElyMediaInfo* out_info) {
    if (!p || !out_info) return ELY_ERR_BAD_ARG;
    if (out_info->struct_size < (int)(sizeof(int) * 3))
        return fail(p, ELY_ERR_BAD_ARG, L"media_info: struct_size not set");
    if (p->state == ELY_STATE_EMPTY)
        return fail(p, ELY_ERR_BAD_STATE, L"media_info: nothing loaded");
    int n = out_info->struct_size;
    if (n > (int)sizeof(ElyMediaInfo)) n = (int)sizeof(ElyMediaInfo);
    int caller_size = out_info->struct_size;
    memcpy(out_info, &p->info, (size_t)n);
    out_info->struct_size = caller_size;
    return ELY_OK;
}

ELY_API int ely_set_video_hwnd(ElyPlayer* p, void* hwnd) {
    if (!p) return ELY_ERR_BAD_ARG;
    p->hwnd = hwnd;
    if (!hwnd) {
        video_out_detach(p->video_out);
        return ELY_OK;
    }
    if (!video_out_attach(p->video_out, hwnd))
        return fail(p, ELY_ERR_VIDEO_TARGET, L"hwnd: attach failed");
    return ELY_OK;
}

ELY_API int ely_resize_video(ElyPlayer* p, int width, int height) {
    if (!p) return ELY_ERR_BAD_ARG;
    if (width < 0 || height < 0)
        return fail(p, ELY_ERR_BAD_ARG, L"resize: negative dimensions");
    p->target_w = width;
    p->target_h = height;
    /* Legal no-op with no attached target, per the locked contract rule. */
    video_out_resize(p->video_out, width, height);
    return ELY_OK;
}

ELY_API const wchar_t* ely_get_last_error(ElyPlayer* p) {
    return p ? p->last_error : L"";
}

}  /* extern "C" */
