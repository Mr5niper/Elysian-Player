#include "video_out.h"

int video_out_attach(VideoOut* v, void* hwnd) {
    if (!v) return 0;
    v->hwnd = hwnd;
    v->attached = hwnd ? 1 : 0;
    return 1;
}

void video_out_detach(VideoOut* v) {
    if (!v) return;
    v->hwnd = nullptr;
    v->attached = 0;
    v->w = 0;
    v->h = 0;
    video_out_clear(v);
}

int video_out_resize(VideoOut* v, int w, int h) {
    if (!v || w < 0 || h < 0) return 0;
    v->w = w;
    v->h = h;
    return 1;
}

int video_out_present(VideoOut* v, const VideoFrame* frame) {
    if (!v || !frame) return 0;
    v->last = *frame;
    v->last_pts = frame->pts;
    v->presents++;
    return 1;
}

void video_out_clear(VideoOut* v) {
    if (!v) return;
    v->last = VideoFrame();
    v->last_pts = 0.0;
    v->presents = 0;
}
