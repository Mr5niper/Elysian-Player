/* test_player_pipeline.cpp - proves the Part C stepped pipeline through the
 * ABI while inspecting engine internals the ABI hides. Linked against the
 * engine objects directly (not the shared library):
 *
 *   g++ -std=c++17 -O2 -o test_pipeline src/real/test_player_pipeline.cpp \
 *       src/real/abi_exports.cpp src/real/player.cpp src/real/clock.cpp \
 *       src/real/queue.cpp src/real/mp4_demux.cpp src/real/aac_decode.cpp \
 *       src/real/h264_decode.cpp src/real/audio_out.cpp src/real/video_out.cpp
 *   ./test_pipeline fixture.mp4
 */
#define ELY_VIDEO_EXPORTS
#include "../../include/elysian_video.h"
#include "player.h"
#include "queue.h"
#include "audio_out.h"
#include "video_out.h"
#include "mp4_demux.h"

#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>

#ifdef _WIN32
#include <windows.h>
static void sleep_ms(int ms) { Sleep(ms); }
#else
#include <time.h>
static void sleep_ms(int ms) {
    struct timespec ts = { ms / 1000, (ms % 1000) * 1000000L };
    nanosleep(&ts, NULL);
}
#endif

int main(int argc, char** argv) {
    if (argc < 2) { fprintf(stderr, "usage: test_pipeline fixture.mp4\n"); return 2; }
    wchar_t wpath[2048];
    mbstowcs(wpath, argv[1], 2047);
    wpath[2047] = 0;

    ElyPlayer* p = ely_create_player();
    assert(p);
    assert(ely_load(p, wpath) == 0);
    assert(ely_play(p) == 0);

    /* pump for a moment of real time: packets must flow into both outputs */
    for (int i = 0; i < 6; i++) { sleep_ms(50); ely_get_state(p); }
    assert(p->audio_out->writes > 0 && "PCM must reach the audio sink");
    assert(p->video_out->presents > 0 && "frames must reach the presenter");
    assert(p->video_out->last.width == 1920 && p->video_out->last.stride == 1920 * 4);
    double pos = ely_get_position(p);
    assert(pos > 0.15 && "position advances on the clock");
    /* presented frames track the clock within the pump lookahead, in real
       and sanitizer builds alike */
    assert(p->video_out->last_pts >= 0.0 &&
           p->video_out->last_pts <= pos + 0.6);
    printf("pipeline flows: %zu PCM writes, %zu presented frames, pos %.2f\n",
           p->audio_out->writes, p->video_out->presents, pos);

    /* presented frames keep pace with the clock, not with call count */
    size_t before = p->video_out->presents;
    double t0 = ely_get_position(p);
    for (int i = 0; i < 50; i++) ely_get_state(p);   /* burst of calls */
    double t1 = ely_get_position(p);
    /* frames presented during the burst must track elapsed clock time
       (30 fps plus lookahead slack), never the call count */
    size_t allowed = (size_t)((t1 - t0) * 30.0) + 4;
    assert(p->video_out->presents - before <= allowed &&
           "pump must be paced by the clock, not by call frequency");
    printf("pump is clock-paced: burst of 50 calls presented %zu frames\n",
           p->video_out->presents - before);

    /* pause freezes; queues survive; nothing new decodes */
    assert(ely_pause(p) == 0);
    size_t wa = p->audio_out->writes, wv = p->video_out->presents;
    sleep_ms(150);
    ely_get_state(p);
    assert(p->audio_out->writes == wa && p->video_out->presents == wv);
    printf("pause: pipeline holds still\n");

    /* seek flushes queues and decoders, stays paused, resumes cleanly */
    assert(ely_seek(p, 5.0) == 0);
    assert(p->audio_q->count == 0 && p->video_q->count == 0 &&
           "seek must flush the packet queues");
    assert(!p->demux_eof);
    assert(ely_get_state(p) == 3);              /* still PAUSED */
    assert(ely_resume(p) == 0);
    sleep_ms(120);
    ely_get_state(p);
    assert(p->video_out->last_pts >= 5.0 - 0.2 &&
           "presented frames must come from after the seek target");
    printf("seek: flushed, resumed, presenting from %.2f\n",
           p->video_out->last_pts);

    /* drained EOF: seek near the end, ENDED only once everything drains */
    assert(ely_seek(p, ely_get_duration(p) - 1.0) == 0);
    assert(!ely_is_finished(p));
    sleep_ms(1150);
    assert(ely_is_finished(p) && "must end at natural EOF");
    assert(player_pipeline_drained(p) && "ENDED requires a drained pipeline");
    assert(p->demux_eof && p->audio_eof && p->video_eof);
    assert(p->audio_q->count == 0 && p->video_q->count == 0);
    printf("EOF: ENDED only with demux EOF and drained queues\n");

    /* restart from ENDED resets the pipeline and plays again */
    assert(ely_play(p) == 0);
    assert(!p->demux_eof && "restart must rewind the demux pump");
    sleep_ms(80);
    ely_get_state(p);
    assert(ely_get_position(p) < 0.5);
    printf("restart from ENDED: pipeline reset and flowing\n");

    /* stop keeps media loaded, clears the pipeline */
    assert(ely_stop(p) == 0);
    assert(p->audio_q->count == 0 && p->video_q->count == 0);
    assert(ely_get_duration(p) > 0.0);
    printf("stop: pipeline cleared, media retained\n");

    /* audio-only file drives the audio path with no video flags in the way */
    if (argc > 2) {
        wchar_t apath[2048];
        mbstowcs(apath, argv[2], 2047);
        apath[2047] = 0;
        assert(ely_load(p, apath) == 0);
        assert(ely_play(p) == 0);
        sleep_ms(120);
        ely_get_state(p);
        assert(p->audio_out->writes > 0);
        assert(p->video_out->presents == 0 &&
               "audio-only media must not touch the presenter");
        assert(ely_seek(p, ely_get_duration(p) - 0.6) == 0);
        assert(ely_play(p) == 0);
        sleep_ms(800);
        assert(ely_is_finished(p));
        printf("audio-only path: flows and drains to ENDED\n");
    }

    /* ---- C.2 additions --------------------------------------------------- */

    /* repeated stop safety */
    assert(ely_load(p, wpath) == 0);
    assert(ely_play(p) == 0);
    sleep_ms(60);
    assert(ely_stop(p) == 0);
    assert(ely_stop(p) == 0 && "second stop must be safe");
    assert(p->audio_q->count == 0 && p->video_q->count == 0);
    assert(ely_get_duration(p) > 0.0);
    printf("repeated stop: safe, queues empty, media retained\n");

    /* explicit pipeline reset safety */
    assert(ely_play(p) == 0);
    sleep_ms(60);
    ely_get_state(p);
    player_reset_pipeline(p);
    assert(p->audio_q->count == 0 && p->video_q->count == 0);
    assert(p->demux_eof == 0 && p->audio_eof == 0 && p->video_eof == 0);
    assert(p->audio_failures == 0 && p->video_failures == 0);
    printf("explicit reset: counts and flags all cleared\n");
    assert(ely_stop(p) == 0);

    /* detached video target safety */
    assert(ely_set_video_hwnd(p, (void*)0x1) == 0);
    assert(p->video_out->attached == 1);
    assert(ely_set_video_hwnd(p, NULL) == 0);
    assert(p->video_out->attached == 0);
    assert(ely_resize_video(p, 640, 360) == 0 &&
           "resize with no target stays a legal no-op");
    printf("video target: attach, detach and detached resize safe\n");

    /* queue pressure: with the target full, fill must neither drop a
       sample nor advance the demux cursor to find that out */
    player_reset_pipeline(p);
    mp4_seek(p->demux, 0.0);
    while (p->video_q->count < p->video_q->cap) {
        Packet dummy;
        memset(&dummy, 0, sizeof(dummy));
        dummy.data = (unsigned char*)malloc(1);
        dummy.size = 1;
        dummy.stream_kind = 2;
        assert(queue_push(p->video_q, dummy));
    }
    {
        size_t vcur = p->demux->video.cursor;
        size_t acur = p->demux->audio.cursor;
        int kind = 0;
        assert(mp4_peek_next_kind(p->demux, &kind));
        if (kind == 2) {
            int filled = player_fill_queues(p, 16);
            assert(filled == 0 && "full target must stop the fill");
            assert(p->demux->video.cursor == vcur &&
                   p->demux->audio.cursor == acur &&
                   "a full target must not cost consumed samples");
        }
        assert(p->video_q->count == p->video_q->cap && "no overfill");
    }
    player_reset_pipeline(p);
    printf("queue pressure: bounded, and peek prevents consumed samples\n");

    /* failure escalation: malformed (zero-size) packets must fail decode
       repeatedly and promote the player to ERROR through settle */
    assert(ely_play(p) == 0);
    for (int i = 0; i < 12; i++) {
        Packet bad;
        memset(&bad, 0, sizeof(bad));
        bad.data = (unsigned char*)malloc(1);
        bad.size = 0;                       /* malformed: no bytes */
        bad.stream_kind = 1;
        bad.pts = 0.0;                      /* always due */
        assert(queue_push(p->audio_q, bad));
    }
    for (int i = 0; i < 4 && ely_get_state(p) != 6; i++) sleep_ms(10);
    assert(ely_get_state(p) == 6 && "repeated decode failure must ERROR");
    assert(wcslen(ely_get_last_error(p)) > 0);
    assert(ely_unload(p) == 0 && ely_get_state(p) == 0 &&
           "unload must recover from ERROR");
    printf("failure escalation: ERROR after repeated decode failure, "
           "unload recovers\n");

    /* real-world file: FFmpeg-muxed, libx264+AAC, end to end */
    if (argc > 3) {
        wchar_t rpath[2048];
        mbstowcs(rpath, argv[3], 2047);
        rpath[2047] = 0;
        assert(ely_load(p, rpath) == 0 &&
               "a real muxer's MP4 must load through the owned demuxer");
        ElyMediaInfo info;
        memset(&info, 0, sizeof(info));
        info.struct_size = (int)sizeof(info);
        assert(ely_get_media_info(p, &info) == 0);
        assert(info.kind == 2 && info.width == 320 && info.height == 240);
        assert(ely_play(p) == 0 &&
               "real avcC and ASC must satisfy the parsing decoders");
        sleep_ms(150);
        ely_get_state(p);
        assert(p->audio_out->writes > 0 && p->video_out->presents > 0);
        assert(ely_get_position(p) > 0.05);
        assert(ely_stop(p) == 0);
        printf("real-world MP4 (libx264 + AAC): loads, plays, flows\n");
    }

    ely_destroy_player(p);
    printf("ALL PIPELINE TESTS PASSED\n");
    return 0;
}
