#pragma once
#include "frame.h"

struct VideoOut {
    void* hwnd = nullptr;
    int w = 0;
    int h = 0;
    int attached = 0;
    int cleared = 1;
    VideoFrame last;
    double last_pts = 0.0;
    size_t presents = 0;
    size_t bytes_presented = 0;
};

int video_out_attach(VideoOut* v, void* hwnd);
void video_out_detach(VideoOut* v);
int video_out_resize(VideoOut* v, int w, int h);
/* Non-const: moves the caller's frame into the retained buffer instead of
 * copying it. See the comment on the definition in video_out.cpp for why
 * this is safe with player_pump()'s only call site. */
int video_out_present(VideoOut* v, VideoFrame* frame);
void video_out_clear(VideoOut* v);
