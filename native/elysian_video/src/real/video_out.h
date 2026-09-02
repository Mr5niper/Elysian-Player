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
int video_out_present(VideoOut* v, const VideoFrame* frame);
void video_out_clear(VideoOut* v);
