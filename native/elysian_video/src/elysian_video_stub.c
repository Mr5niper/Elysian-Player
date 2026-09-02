/* elysian_video_stub.c - the Phase 1 engine: no demuxing, no decoding, no
 * output, but the COMPLETE contract. The state machine is the real one, the
 * position clock genuinely runs, pauses freeze it, seeks move it, and the
 * file "ends" when the clock reaches the fake duration. That makes the shell
 * integratable and testable against exact contract semantics before a single
 * codec exists; later phases replace the middle of this file, not the edges.
 *
 * Compiles as the Windows DLL (BUILD_DLL.bat) and, unchanged, as a Linux .so
 * (Makefile) so the ABI and Python bindings are provable off-Windows too.
 */
#define ELY_VIDEO_EXPORTS
#include "../include/elysian_video.h"

#include <stdlib.h>
#include <string.h>
#include <stdio.h>

#ifdef _WIN32
  #include <windows.h>
  #include <io.h>
#else
  #include <time.h>
  #include <unistd.h>
#endif

#define ERRBUF 256
#define STUB_DURATION 10.0     /* every loaded file pretends to be 10 s */

struct ElyPlayer {
    int state;                 /* ElyState */
    wchar_t path[1024];
    ElyMediaInfo info;
    float volume;
    void* hwnd;
    int target_w, target_h;
    /* Simulated playback clock: position = base_pos + (now - base_wall)
       while PLAYING; frozen at base_pos otherwise. */
    double base_pos;
    double base_wall;
    wchar_t last_error[ERRBUF];
};

static double now_seconds(void) {
#ifdef _WIN32
    static LARGE_INTEGER freq;
    LARGE_INTEGER c;
    if (!freq.QuadPart) QueryPerformanceFrequency(&freq);
    QueryPerformanceCounter(&c);
    return (double)c.QuadPart / (double)freq.QuadPart;
#else
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
#endif
}

static void set_error(ElyPlayer* p, const wchar_t* msg) {
    if (!p) return;
    wcsncpy(p->last_error, msg, ERRBUF - 1);
    p->last_error[ERRBUF - 1] = 0;
}

static int fail(ElyPlayer* p, int code, const wchar_t* msg) {
    set_error(p, msg);
    return code;
}

/* Lazily promote PLAYING to ENDED when the simulated clock passes the
 * duration. Every getter and command funnels through this, so the caller
 * observes ENDED exactly as the contract describes: position pinned at
 * duration, is_finished true, no early EOF while "buffered media" remains
 * (the simulation has none, so reaching the duration IS drained). */
static void settle_clock(ElyPlayer* p) {
    if (p->state != ELY_STATE_PLAYING) return;
    double pos = p->base_pos + (now_seconds() - p->base_wall);
    if (pos >= p->info.duration) {
        p->base_pos = p->info.duration;
        p->state = ELY_STATE_ENDED;
    }
}

static double current_pos(ElyPlayer* p) {
    settle_clock(p);
    if (p->state == ELY_STATE_PLAYING)
        return p->base_pos + (now_seconds() - p->base_wall);
    if (p->state == ELY_STATE_EMPTY || p->state == ELY_STATE_STOPPED)
        return 0.0;
    return p->base_pos;
}

static int file_exists(const wchar_t* path) {
#ifdef _WIN32
    return _waccess(path, 0) == 0;
#else
    char narrow[4096];
    size_t n = wcstombs(narrow, path, sizeof(narrow) - 1);
    if (n == (size_t)-1) return 0;
    narrow[n] = 0;
    return access(narrow, 0) == 0;
#endif
}

static int has_ext(const wchar_t* path, const wchar_t* ext) {
    size_t lp = wcslen(path), le = wcslen(ext);
    if (le >= lp) return 0;
    const wchar_t* tail = path + lp - le;
    for (size_t i = 0; i < le; i++) {
        wchar_t a = tail[i], b = ext[i];
        if (a >= L'A' && a <= L'Z') a += 32;
        if (a != b) return 0;
    }
    return 1;
}

ELY_API int ely_abi_version(void) { return ELY_ABI_VERSION; }

ELY_API ElyPlayer* ely_create_player(void) {
    ElyPlayer* p = (ElyPlayer*)calloc(1, sizeof(ElyPlayer));
    if (!p) return NULL;
    p->state = ELY_STATE_EMPTY;
    p->volume = 1.0f;
    return p;
}

ELY_API void ely_destroy_player(ElyPlayer* p) {
    free(p);
}

ELY_API int ely_load(ElyPlayer* p, const wchar_t* path) {
    if (!p) return ELY_ERR_BAD_ARG;
    if (!path || !path[0])
        return fail(p, ELY_ERR_BAD_ARG, L"load: empty path");
    if (!file_exists(path))
        return fail(p, ELY_ERR_NOT_FOUND, L"load: file not found");

    /* Implicit unload of any previous media, per contract. */
    memset(&p->info, 0, sizeof(p->info));
    wcsncpy(p->path, path, 1023);
    p->path[1023] = 0;

    p->info.struct_size = (int)sizeof(ElyMediaInfo);
    p->info.duration = STUB_DURATION;
    if (has_ext(path, L".mp4") || has_ext(path, L".m4v") ||
        has_ext(path, L".mov") || has_ext(path, L".mkv")) {
        p->info.kind = ELY_MEDIA_VIDEO;
        p->info.has_video = 1;
        p->info.has_audio = 1;
        p->info.width = 1920;
        p->info.height = 1080;
        p->info.frame_rate = 30.0;
        p->info.audio_sample_rate = 48000;
        p->info.audio_channels = 2;
    } else if (has_ext(path, L".mp3") || has_ext(path, L".flac") ||
               has_ext(path, L".wav") || has_ext(path, L".ogg") ||
               has_ext(path, L".m4a") || has_ext(path, L".aac")) {
        p->info.kind = ELY_MEDIA_AUDIO;
        p->info.has_audio = 1;
        p->info.audio_sample_rate = 44100;
        p->info.audio_channels = 2;
    } else {
        p->info.kind = ELY_MEDIA_UNKNOWN;
        return fail(p, ELY_ERR_UNSUPPORTED, L"load: unsupported container");
    }

    p->base_pos = 0.0;
    p->state = ELY_STATE_LOADED;
    /* Deliberately no error-buffer clear: per contract, last_error keeps
       describing the most recent FAILURE on this handle; success never
       clears it. One rule, no special cases. */
    return ELY_OK;
}

ELY_API int ely_unload(ElyPlayer* p) {
    if (!p) return ELY_ERR_BAD_ARG;
    p->state = ELY_STATE_EMPTY;
    p->path[0] = 0;
    memset(&p->info, 0, sizeof(p->info));
    p->base_pos = 0.0;
    return ELY_OK;
}

ELY_API int ely_play(ElyPlayer* p) {
    if (!p) return ELY_ERR_BAD_ARG;
    settle_clock(p);
    switch (p->state) {
    case ELY_STATE_LOADED:
        break;                              /* start from current (0) */
    case ELY_STATE_PAUSED:
        break;                              /* resume from frozen pos */
    case ELY_STATE_STOPPED:
    case ELY_STATE_ENDED:
        p->base_pos = 0.0;                  /* restart */
        break;
    case ELY_STATE_PLAYING:
        return ELY_OK;                      /* idempotent */
    default:
        return fail(p, ELY_ERR_BAD_STATE, L"play: nothing loaded");
    }
    p->base_wall = now_seconds();
    p->state = ELY_STATE_PLAYING;
    return ELY_OK;
}

ELY_API int ely_pause(ElyPlayer* p) {
    if (!p) return ELY_ERR_BAD_ARG;
    settle_clock(p);
    if (p->state != ELY_STATE_PLAYING)
        return fail(p, ELY_ERR_BAD_STATE, L"pause: not playing");
    p->base_pos = p->base_pos + (now_seconds() - p->base_wall);
    p->state = ELY_STATE_PAUSED;
    return ELY_OK;
}

ELY_API int ely_resume(ElyPlayer* p) {
    if (!p) return ELY_ERR_BAD_ARG;
    if (p->state != ELY_STATE_PAUSED)
        return fail(p, ELY_ERR_BAD_STATE, L"resume: not paused");
    return ely_play(p);
}

ELY_API int ely_stop(ElyPlayer* p) {
    if (!p) return ELY_ERR_BAD_ARG;
    if (p->state == ELY_STATE_EMPTY)
        return fail(p, ELY_ERR_BAD_STATE, L"stop: nothing loaded");
    p->base_pos = 0.0;
    p->state = ELY_STATE_STOPPED;           /* file stays loaded */
    return ELY_OK;
}

ELY_API int ely_seek(ElyPlayer* p, double seconds) {
    if (!p) return ELY_ERR_BAD_ARG;
    settle_clock(p);
    if (p->state == ELY_STATE_EMPTY)
        return fail(p, ELY_ERR_BAD_STATE, L"seek: nothing loaded");
    if (seconds < 0.0) seconds = 0.0;
    if (seconds > p->info.duration) seconds = p->info.duration;
    p->base_pos = seconds;
    p->base_wall = now_seconds();
    /* Paused stays paused at the new position; playing keeps playing;
       an ENDED player seeked backwards becomes PAUSED, not magically
       playing. Stopped stays stopped with the new resume point. */
    if (p->state == ELY_STATE_ENDED && seconds < p->info.duration)
        p->state = ELY_STATE_PAUSED;
    return ELY_OK;
}

ELY_API int ely_set_volume(ElyPlayer* p, float volume) {
    if (!p) return ELY_ERR_BAD_ARG;
    if (volume < 0.0f) volume = 0.0f;
    if (volume > 1.0f) volume = 1.0f;
    p->volume = volume;
    return ELY_OK;
}

ELY_API float ely_get_volume(ElyPlayer* p) {
    return p ? p->volume : 0.0f;
}

ELY_API double ely_get_position(ElyPlayer* p) {
    if (!p) return 0.0;
    double pos = current_pos(p);
    if (pos > p->info.duration) pos = p->info.duration;
    return pos;
}

ELY_API double ely_get_duration(ElyPlayer* p) {
    if (!p || p->state == ELY_STATE_EMPTY) return 0.0;
    return p->info.duration;
}

ELY_API int ely_is_playing(ElyPlayer* p) {
    if (!p) return 0;
    settle_clock(p);
    return p->state == ELY_STATE_PLAYING;
}

ELY_API int ely_is_paused(ElyPlayer* p) {
    if (!p) return 0;
    settle_clock(p);
    return p->state == ELY_STATE_PAUSED;
}

ELY_API int ely_is_active(ElyPlayer* p) {
    if (!p) return 0;
    settle_clock(p);
    return p->state == ELY_STATE_PLAYING || p->state == ELY_STATE_PAUSED;
}

ELY_API int ely_is_finished(ElyPlayer* p) {
    if (!p) return 0;
    settle_clock(p);
    return p->state == ELY_STATE_ENDED;
}

ELY_API int ely_get_state(ElyPlayer* p) {
    if (!p) return ELY_STATE_ERROR;
    settle_clock(p);
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
    p->hwnd = hwnd;                          /* NULL detaches */
    return ELY_OK;
}

ELY_API int ely_resize_video(ElyPlayer* p, int width, int height) {
    if (!p) return ELY_ERR_BAD_ARG;
    if (width < 0 || height < 0)
        return fail(p, ELY_ERR_BAD_ARG, L"resize: negative dimensions");
    p->target_w = width;
    p->target_h = height;
    return ELY_OK;
}

ELY_API const wchar_t* ely_get_last_error(ElyPlayer* p) {
    return p ? p->last_error : L"";
}
