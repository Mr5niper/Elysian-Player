#pragma once
/* Milestone 2 seam: frame presenter. The skeleton included <windows.h>
 * unconditionally, which breaks the portable test build; the handle is a
 * void* here and the Windows implementation casts to HWND internally. */
struct VideoOut { void* hwnd; int w; int h; };
int video_out_attach(VideoOut* v, void* hwnd);
void video_out_detach(VideoOut* v);
int video_out_resize(VideoOut* v, int w, int h);
