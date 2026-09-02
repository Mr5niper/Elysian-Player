/* elysian_video.h - the frozen ABI between the Elysian shell and the owned
 * media engine. This header IS the contract: implementations may change
 * behind it (stub, hand-rolled codecs, or a platform decoder), callers may
 * not assume anything not written here or in CONTRACT.md.
 *
 * ABI rules:
 *  - C only, no name mangling. Plain ints, doubles, pointers.
 *  - Enum values are pinned forever; new values may be appended only.
 *  - New functions may be appended; existing signatures never change.
 *  - ElyMediaInfo grows only at the end, guarded by the struct_size
 *    handshake below.
 *  - Every call on one ElyPlayer must be serialized by the caller. The
 *    Elysian shell already funnels playback through one worker thread, so
 *    this costs it nothing. The engine may use internal threads freely.
 *  - Paths are wchar_t. Narrow-char paths through the C runtime are how the
 *    shell once lost waveforms for every non-ASCII filename; the engine does
 *    not get the chance to repeat that.
 */
#pragma once

#ifdef _WIN32
  #ifdef ELY_VIDEO_EXPORTS
    #define ELY_API __declspec(dllexport)
  #else
    #define ELY_API __declspec(dllimport)
  #endif
#else
  #define ELY_API __attribute__((visibility("default")))
#endif

#include <stddef.h>
#include <wchar.h>

#ifdef __cplusplus
extern "C" {
#endif

#define ELY_ABI_VERSION 1

typedef struct ElyPlayer ElyPlayer;

typedef enum ElyState {
    ELY_STATE_EMPTY = 0,
    ELY_STATE_LOADED = 1,
    ELY_STATE_PLAYING = 2,
    ELY_STATE_PAUSED = 3,
    ELY_STATE_STOPPED = 4,
    ELY_STATE_ENDED = 5,
    ELY_STATE_ERROR = 6
} ElyState;

typedef enum ElyMediaKind {
    ELY_MEDIA_UNKNOWN = 0,
    ELY_MEDIA_AUDIO = 1,
    ELY_MEDIA_VIDEO = 2
} ElyMediaKind;

typedef enum ElyResult {
    ELY_OK = 0,
    ELY_ERR_GENERIC = 1,
    ELY_ERR_BAD_ARG = 2,
    ELY_ERR_NOT_FOUND = 3,
    ELY_ERR_UNSUPPORTED = 4,
    ELY_ERR_BAD_CONTAINER = 5,
    ELY_ERR_BAD_STREAM = 6,
    ELY_ERR_DECODE = 7,
    ELY_ERR_AUDIO_DEVICE = 8,
    ELY_ERR_VIDEO_TARGET = 9,
    ELY_ERR_SEEK = 10,
    ELY_ERR_BAD_STATE = 11
} ElyResult;

/* Callers MUST set struct_size = sizeof(ElyMediaInfo) before calling
 * ely_get_media_info. The engine fills at most struct_size bytes, so an old
 * caller keeps working against a newer engine that appended fields, and a
 * new caller against an old engine simply gets the fields that exist. */
typedef struct ElyMediaInfo {
    int struct_size;
    int has_audio;
    int has_video;
    int width;
    int height;
    double duration;
    double frame_rate;
    int audio_sample_rate;
    int audio_channels;
    int kind;               /* ElyMediaKind, int-typed to pin the ABI width */
} ElyMediaInfo;

/* Returns ELY_ABI_VERSION of the built library. Bindings refuse to run
 * against a library whose major version they do not know. */
ELY_API int ely_abi_version(void);

ELY_API ElyPlayer* ely_create_player(void);
ELY_API void ely_destroy_player(ElyPlayer* p);

ELY_API int ely_load(ElyPlayer* p, const wchar_t* path);
ELY_API int ely_unload(ElyPlayer* p);

ELY_API int ely_play(ElyPlayer* p);
ELY_API int ely_pause(ElyPlayer* p);
ELY_API int ely_resume(ElyPlayer* p);
ELY_API int ely_stop(ElyPlayer* p);
ELY_API int ely_seek(ElyPlayer* p, double seconds);

ELY_API int ely_set_volume(ElyPlayer* p, float volume);   /* clamped 0..1 */
ELY_API float ely_get_volume(ElyPlayer* p);

ELY_API double ely_get_position(ElyPlayer* p);
ELY_API double ely_get_duration(ElyPlayer* p);

ELY_API int ely_is_playing(ElyPlayer* p);
ELY_API int ely_is_paused(ElyPlayer* p);
ELY_API int ely_is_active(ElyPlayer* p);
ELY_API int ely_is_finished(ElyPlayer* p);
ELY_API int ely_get_state(ElyPlayer* p);                  /* ElyState */

ELY_API int ely_get_media_info(ElyPlayer* p, ElyMediaInfo* out_info);

/* hwnd is void* so the header stays platform-neutral; NULL detaches. */
ELY_API int ely_set_video_hwnd(ElyPlayer* p, void* hwnd);
ELY_API int ely_resize_video(ElyPlayer* p, int width, int height);

/* Valid until the next failing call on the same player. Never NULL for a
 * valid player; empty string when no error has occurred. */
ELY_API const wchar_t* ely_get_last_error(ElyPlayer* p);

#ifdef __cplusplus
}
#endif
