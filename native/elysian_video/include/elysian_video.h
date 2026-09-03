/* elysian_video.h - the frozen ABI between the Elysian shell and the native
 * media engine. This header IS the contract: implementations may change
 * behind it (stub, owned codecs, or FFmpeg-backed, as has now happened),
 * callers may
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
 *    this costs it nothing. The engine may use internal threads freely, but
 *    getters must never block on pipeline work, and the engine never calls
 *    back into caller code from its own threads. State is pulled, not
 *    pushed.
 *  - Paths are wchar_t. Narrow-char paths through the C runtime are how the
 *    shell once lost waveforms for every non-ASCII filename; the engine does
 *    not get the chance to repeat that. v1 accepts local filesystem paths
 *    only: no URLs, no device paths, no pipes or streams.
 *  - Capability queries live in ElyMediaInfo via ely_get_media_info; there
 *    are deliberately no separate has_audio/has_video/kind functions, and
 *    textual metadata (title/artist/album) is out of scope for ABI v1: the
 *    shell owns display metadata, as it already does for audio.
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

/* play: LOADED starts from current position, PAUSED resumes, STOPPED and
 * ENDED restart from zero, PLAYING is an idempotent success that does not
 * rewind, EMPTY fails BAD_STATE.
 * stop: keeps the media loaded, resets position to zero, state STOPPED;
 * only EMPTY rejects it.
 * seek: clamps to [0, duration]; paused stays paused at the target, playing
 * keeps playing, and seeking an ENDED player to before the end lands it
 * PAUSED, never silently playing. */
ELY_API int ely_play(ElyPlayer* p);
ELY_API int ely_pause(ElyPlayer* p);
ELY_API int ely_resume(ElyPlayer* p);
ELY_API int ely_stop(ElyPlayer* p);
ELY_API int ely_seek(ElyPlayer* p, double seconds);

ELY_API int ely_set_volume(ElyPlayer* p, float volume);   /* clamped 0..1 */
ELY_API float ely_get_volume(ElyPlayer* p);

/* position is zero in EMPTY and STOPPED; duration remains available in any
 * loaded state, STOPPED included, and is zero only in EMPTY. */
ELY_API double ely_get_position(ElyPlayer* p);
ELY_API double ely_get_duration(ElyPlayer* p);

ELY_API int ely_is_playing(ElyPlayer* p);
ELY_API int ely_is_paused(ElyPlayer* p);
ELY_API int ely_is_active(ElyPlayer* p);
ELY_API int ely_is_finished(ElyPlayer* p);
ELY_API int ely_get_state(ElyPlayer* p);                  /* ElyState */

ELY_API int ely_get_media_info(ElyPlayer* p, ElyMediaInfo* out_info);

/* hwnd is void* so the header stays platform-neutral; NULL detaches.
 * ely_resize_video with no target attached is a legal no-op success: the
 * shell may report layout changes without tracking attachment state. */
ELY_API int ely_set_video_hwnd(ElyPlayer* p, void* hwnd);
ELY_API int ely_resize_video(ElyPlayer* p, int width, int height);

/* Describes the most recent FAILURE on this handle. Success never clears
 * it; the next failing call replaces it. Empty string until the first
 * failure. Never NULL: a NULL player yields the empty string, and for a
 * valid player the pointer is valid until the next failing call on it. */
ELY_API const wchar_t* ely_get_last_error(ElyPlayer* p);

#ifdef __cplusplus
}
#endif
