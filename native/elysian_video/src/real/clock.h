#pragma once

/* Monotonic playback clock. Position = base_pos while frozen, plus elapsed
 * wall time while running. Owns nothing else; EOF policy lives in the
 * player, which asks for the unclamped position and decides. */
struct PlaybackClock {
    double base_pos;
    double base_wall;
    int running;
};

void clock_reset(PlaybackClock* c, double pos);
void clock_play(PlaybackClock* c);
void clock_pause(PlaybackClock* c);
void clock_seek(PlaybackClock* c, double pos);
double clock_position(const PlaybackClock* c, double duration, int clamp);
