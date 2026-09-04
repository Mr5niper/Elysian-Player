#pragma once
#include "../../include/elysian_video.h"

#include <atomic>
#include <condition_variable>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

struct Mp4Demux;
struct AacDecoder;
struct H264Decoder;
struct AudioOut;
struct VideoOut;
struct PlaybackClock;
struct PacketQueue;

/* Threading model, added to fix a real, self-inflicted contract violation:
 * every ABI getter used to call player_settle(), which can run player_pump()
 * and block for real wall-clock time doing actual decode work whenever the
 * pipeline has catching up to do. That directly contradicts this header's
 * own documented rule two files up ("getters must never block on pipeline
 * work"), and it meant a single slow catch-up pass on the Python shell's
 * one worker thread could stall the next queued UI command behind it,
 * whatever that command was.
 *
 * The fix: a dedicated background thread, started once in player_create()
 * and joined in player_destroy(), owns calling player_settle() on its own
 * cadence, independent of anything any caller does. Every getter now reads
 * only the small "published" snapshot below (state/position/playing/
 * paused/finished/volume), all plain std::atomic values written under
 * ctl_mu and read without it - lock-free, and genuinely fast regardless of
 * what the pump thread is doing at that instant.
 *
 * This introduces the one thing that was never previously true: a second
 * thread now touches the same ElyPlayer the caller's own thread does. The
 * ABI's caller-serialization rule ("every call on one ElyPlayer must be
 * serialized by the caller") still holds for the CALLER's own calls to each
 * other; it says nothing about the engine's own internal threads, which the
 * contract explicitly permits ("the engine may use internal threads
 * freely"). What it does require, and what ctl_mu exists to provide, is
 * that the pump thread and any single caller-thread mutating call (play,
 * pause, seek, stop, load, unload, set_video_hwnd, resize_video, set_volume)
 * never touch the same non-atomic pipeline state (queues, decoders, demux
 * cursor, VideoOut's attached target) at the same instant - none of that
 * state is otherwise synchronized at all. Every one of those mutating
 * calls acquires ctl_mu for its full body via player_guard(); the pump
 * thread holds it for the duration of one settle-and-publish cycle and
 * releases it while sleeping, via condition_variable::wait_for, so a
 * caller's mutating call is never blocked longer than one in-flight pump
 * step. */
struct ElyPlayer {
    int state;                  /* ElyState; authoritative, ctl_mu-guarded */
    std::wstring path;
    ElyMediaInfo info;
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

    /* ---- Part C pipeline state, all ctl_mu-guarded ---------------------- */
    PacketQueue* audio_q;
    PacketQueue* video_q;
    std::vector<unsigned char> demux_buf;
    int demux_eof;
    int audio_ready;
    int video_ready;
    int audio_eof;
    int video_eof;
    /* Consecutive decode failures per path; the pump escalates past a
     * threshold and settle promotes the player to ERROR. */
    int audio_failures;
    int video_failures;

    /* ---- published snapshot: lock-free reads, ctl_mu-guarded writes ----- */
    std::atomic<int> pub_state{ELY_STATE_EMPTY};
    std::atomic<double> pub_position{0.0};
    std::atomic<int> pub_playing{0};
    std::atomic<int> pub_paused{0};
    std::atomic<int> pub_finished{0};
    /* Volume is read (ely_get_volume) and written (ely_set_volume) from
     * whatever thread the caller happens to be on; making it atomic costs
     * nothing and closes what would otherwise be a real, if low-
     * consequence, race on a plain float. */
    std::atomic<float> pub_volume{1.0f};

    /* ---- background pump thread ----------------------------------------- */
    std::mutex ctl_mu;
    std::condition_variable ctl_cv;
    std::thread pump_thread;
    bool stop_requested = false;
};

ElyPlayer* player_create(void);
void player_destroy(ElyPlayer* p);
void player_set_error(ElyPlayer* p, const wchar_t* msg);

/* Lazily promote PLAYING to ENDED. Since Part C, ENDED requires the clock
 * at the duration AND the pipeline drained (demux EOF, queues empty,
 * decoders flushed through), matching the contract's drained-EOF rule.
 * Called only from the pump thread now, and from player_guard()-holding
 * ABI calls that need settle()'s side effects applied before they act on
 * current state (matching the exact calls that ran it before this
 * change) - never from a getter. */
void player_settle(ElyPlayer* p);

/* Writes the current authoritative state into the pub_* atomics. Callers
 * must hold ctl_mu. */
void player_publish_locked(ElyPlayer* p);

/* Acquires ctl_mu for the caller's ABI call body. RAII: the lock releases
 * automatically when the returned guard goes out of scope. */
std::unique_lock<std::mutex> player_guard(ElyPlayer* p);

/* Part C internals: not ABI, used by abi_exports.cpp and tests only.
 * Callers must hold ctl_mu (true for both the pump thread and every
 * mutating ABI call via player_guard()). */
int player_prepare_pipeline(ElyPlayer* p);
void player_reset_pipeline(ElyPlayer* p);
int player_fill_queues(ElyPlayer* p, int max_packets);
int player_pump(ElyPlayer* p);
int player_pipeline_drained(const ElyPlayer* p);
