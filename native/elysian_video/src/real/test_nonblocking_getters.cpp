/* test_nonblocking_getters.cpp - proves the actual point of the threading
 * rewrite: ely_get_position/get_state/is_playing/is_paused/is_active/
 * is_finished must return in microseconds, from any thread, regardless of
 * what the background pump thread is doing at that instant. Before this
 * change, every one of those getters called player_settle() directly and
 * could block for however long a decode catch-up pass took - the exact
 * thing that made a single UI click wait behind an unrelated slow pump
 * call on the Python shell's own worker thread.
 *
 * This does not decode-test anything test_player_pipeline.cpp doesn't
 * already cover; it specifically times getter calls while a real video is
 * actively playing and asserts the total time for a large burst of them
 * stays far below what even one blocking pump call would cost.
 *
 *   g++ -std=c++17 -O2 -pthread `pkg-config --cflags libavformat libavcodec \
 *       libavutil libswscale libswresample` \
 *       -o test_nonblocking_getters src/real/test_nonblocking_getters.cpp \
 *       src/real/abi_exports.cpp src/real/player.cpp src/real/clock.cpp \
 *       src/real/queue.cpp src/real/mp4_demux.cpp src/real/aac_decode.cpp \
 *       src/real/h264_decode.cpp src/real/audio_out.cpp src/real/video_out.cpp \
 *       `pkg-config --libs libavformat libavcodec libavutil libswscale \
 *       libswresample`
 *   ./test_nonblocking_getters fixture.mp4
 */
#define ELY_VIDEO_EXPORTS
#include "../../include/elysian_video.h"

#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <wchar.h>

#ifdef _WIN32
#include <windows.h>
static void sleep_ms(int ms) { Sleep(ms); }
static double now_seconds(void) {
    static LARGE_INTEGER freq;
    LARGE_INTEGER c;
    if (!freq.QuadPart) QueryPerformanceFrequency(&freq);
    QueryPerformanceCounter(&c);
    return (double)c.QuadPart / (double)freq.QuadPart;
}
#else
#include <time.h>
static void sleep_ms(int ms) {
    struct timespec ts = { ms / 1000, (ms % 1000) * 1000000L };
    nanosleep(&ts, NULL);
}
static double now_seconds(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}
#endif

int main(int argc, char** argv) {
    if (argc < 2) {
        fprintf(stderr, "usage: test_nonblocking_getters fixture.mp4\n");
        return 2;
    }
    wchar_t wpath[2048];
    mbstowcs(wpath, argv[1], 2047);
    wpath[2047] = 0;

    ElyPlayer* p = ely_create_player();
    assert(p);
    assert(ely_load(p, wpath) == 0);
    assert(ely_play(p) == 0);

    /* Let the pump thread get into a steady rhythm of real decode work
     * before measuring anything, so the burst below lands while it is
     * genuinely busy, not during the first idle instant after play(). */
    sleep_ms(200);

    /* A large burst, deliberately overlapping with continuous real
     * decode: the pump thread is actively calling player_settle() on its
     * own 5ms cadence throughout this entire loop. If any getter still
     * blocked on pipeline work the way it did before this change, one
     * slow catch-up pass hitting mid-burst would show up immediately as
     * a multi-millisecond-or-worse outlier, and the total would balloon
     * well past what 20,000 atomic loads should ever cost. */
    const int N = 20000;
    double t0 = now_seconds();
    double worst = 0.0;
    for (int i = 0; i < N; i++) {
        double call_t0 = now_seconds();
        volatile int state = ely_get_state(p);
        volatile double pos = ely_get_position(p);
        volatile int playing = ely_is_playing(p);
        volatile int paused = ely_is_paused(p);
        volatile int active = ely_is_active(p);
        volatile int finished = ely_is_finished(p);
        (void)state; (void)pos; (void)playing; (void)paused;
        (void)active; (void)finished;
        double elapsed = now_seconds() - call_t0;
        if (elapsed > worst) worst = elapsed;
    }
    double total = now_seconds() - t0;
    double avg_us = (total / N) * 1e6;
    double worst_us = worst * 1e6;

    printf("nonblocking getters: %d rounds of 6 calls in %.4fs "
          "(avg %.2fus/round, worst single round %.2fus)\n",
          N, total, avg_us, worst_us);

    /* Generous by a wide margin on purpose: this only needs to catch the
     * class of bug this test exists for (a getter that still blocks on
     * real decode work, which costs milliseconds at minimum, often much
     * more), not to pin down exact atomic-load performance, which will
     * legitimately vary across machines and sanitizer instrumentation. */
    assert(avg_us < 200.0 &&
          "getters must average well under real decode-pump timescales");
    assert(worst_us < 5000.0 &&
          "no single getter round may cost anywhere near a pump catch-up");

    /* Sanity: the engine was, in fact, actually doing real work the whole
     * time this ran, so the absence of blocking above is not just because
     * there was nothing to block on. */
    assert(ely_is_playing(p));
    assert(ely_get_position(p) > 0.1);

    ely_destroy_player(p);
    printf("ALL NONBLOCKING-GETTER TESTS PASSED\n");
    return 0;
}
