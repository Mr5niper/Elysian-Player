#include "video_out.h"

#ifdef _WIN32
#include <windows.h>
#endif

/* Part E: real painting, in place of the retained-state-only tracking
 * this replaces. video_out_present blits directly via GetDC/StretchDIBits
 * rather than routing through a window procedure: this HWND's class
 * (registered in elysian/video_host.py) does not special-case WM_PAINT,
 * so there is nothing to hook into, and a direct blit on every decoded
 * frame is the simpler, standard approach for this kind of embedded
 * renderer. video_out_resize re-blits the retained last frame so a window
 * resize does not show stale or blank content until the next frame
 * decodes. Off-Windows this stays exactly the state-tracking behavior it
 * always was, which is what keeps this file's own logic testable in the
 * sandbox; only the paint call itself is Windows-only. */

#ifdef _WIN32
static void paint(VideoOut* v) {
    if (!v->hwnd || !v->attached || v->last.pixels.empty()) return;
    HWND hwnd = static_cast<HWND>(v->hwnd);
    HDC hdc = GetDC(hwnd);
    if (!hdc) return;

    /* HALFTONE gives StretchDIBits a real interpolated scale instead of
     * its default nearest-neighbor-ish COLORONCOLOR behavior, which is a
     * real, standalone contributor to soft/blocky-looking scaled video,
     * independent of anything the decoder does. SetBrushOrgEx is required
     * immediately after selecting HALFTONE per Microsoft's own
     * documentation (it resets the dithering origin GDI uses internally
     * for the mode); omitting it is a common mistake that leaves the
     * stretch mode set but subtly misaligned. Cheap enough to always set
     * before every blit rather than tracking whether it is already
     * selected on this HDC, since GetDC can hand back a fresh HDC state
     * on some drivers. */
    SetStretchBltMode(hdc, HALFTONE);
    SetBrushOrgEx(hdc, 0, 0, nullptr);

    BITMAPINFO bmi;
    ZeroMemory(&bmi, sizeof(bmi));
    bmi.bmiHeader.biSize = sizeof(BITMAPINFOHEADER);
    bmi.bmiHeader.biWidth = v->last.width;
    bmi.bmiHeader.biHeight = -v->last.height;   /* negative: top-down DIB,
                                                  * matching decode order */
    bmi.bmiHeader.biPlanes = 1;
    bmi.bmiHeader.biBitCount = 32;
    bmi.bmiHeader.biCompression = BI_RGB;

    int target_w = v->w > 0 ? v->w : v->last.width;
    int target_h = v->h > 0 ? v->h : v->last.height;
    StretchDIBits(hdc, 0, 0, target_w, target_h,
                 0, 0, v->last.width, v->last.height,
                 v->last.pixels.data(), &bmi, DIB_RGB_COLORS, SRCCOPY);
    ReleaseDC(hwnd, hdc);
}
#endif

int video_out_attach(VideoOut* v, void* hwnd) {
    if (!v) return 0;
    v->hwnd = hwnd;
    v->attached = hwnd ? 1 : 0;
#ifdef _WIN32
    if (v->attached) paint(v);
#endif
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
#ifdef _WIN32
    paint(v);   /* re-blit the retained frame at the new size immediately,
                 * rather than leaving the old size on screen until the
                 * next decoded frame */
#endif
    return 1;
}

int video_out_present(VideoOut* v, VideoFrame* frame) {
    /* Non-const: takes ownership of the caller's frame via move rather
     * than copying it. player_pump()'s only call site (a locally-owned,
     * never-reused VideoFrame right before this call returns) makes this
     * safe - the caller has nothing left to do with *frame afterward
     * either way. Full BGRA frame copies here were a real, avoidable
     * cost on every single presented frame; this removes one of the two
     * copies in that path entirely (decode's own output buffer is the
     * other, unavoidable one). */
    if (!v || !frame) return 0;
    v->last_pts = frame->pts;
    v->presents++;
    v->bytes_presented += frame->pixels.size();
    v->last = std::move(*frame);
    v->cleared = 0;
#ifdef _WIN32
    paint(v);
#endif
    return 1;
}

void video_out_clear(VideoOut* v) {
    if (!v) return;
    v->last = VideoFrame();
    v->last_pts = 0.0;
    v->presents = 0;
    v->bytes_presented = 0;
    v->cleared = 1;
#ifdef _WIN32
    if (v->hwnd && v->attached) {
        HWND hwnd = static_cast<HWND>(v->hwnd);
        HDC hdc = GetDC(hwnd);
        if (hdc) {
            RECT r; GetClientRect(hwnd, &r);
            FillRect(hdc, &r, (HBRUSH)GetStockObject(BLACK_BRUSH));
            ReleaseDC(hwnd, hdc);
        }
    }
#endif
}
