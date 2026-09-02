#include "video_out.h"
int video_out_attach(VideoOut* v, void* hwnd) { v->hwnd = hwnd; return 1; }
void video_out_detach(VideoOut* v) { v->hwnd = 0; v->w = v->h = 0; }
int video_out_resize(VideoOut* v, int w, int h) { v->w = w; v->h = h; return 1; }
