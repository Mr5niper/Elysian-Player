#include "clock.h"

#ifdef _WIN32
#include <windows.h>
#else
#include <time.h>
#endif

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

void clock_reset(PlaybackClock* c, double pos) {
    c->base_pos = pos;
    c->base_wall = now_seconds();
    c->running = 0;
}

void clock_play(PlaybackClock* c) {
    if (!c->running) {
        c->base_wall = now_seconds();
        c->running = 1;
    }
}

void clock_pause(PlaybackClock* c) {
    if (c->running) {
        c->base_pos += now_seconds() - c->base_wall;
        c->running = 0;
    }
}

void clock_seek(PlaybackClock* c, double pos) {
    /* Keeps the running/frozen state: a paused clock stays frozen at the
     * new position, a running one keeps running from it. */
    c->base_pos = pos;
    c->base_wall = now_seconds();
}

double clock_position(const PlaybackClock* c, double duration, int clamp) {
    double p = c->base_pos;
    if (c->running) p += now_seconds() - c->base_wall;
    if (clamp) {
        if (p < 0.0) p = 0.0;
        if (duration > 0.0 && p > duration) p = duration;
    }
    return p;
}
