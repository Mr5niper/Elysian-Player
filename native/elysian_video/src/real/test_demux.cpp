/* test_demux.cpp - ground-truth checks on the FFmpeg-backed demuxer:
 * classification, geometry, codec-config extraction, the full interleaved
 * walk, seek-to-keyframe behavior, and damaged-container handling.
 *
 * Part E: the demuxer's internals are libavformat now, so there is no
 * exposed per-track sample table or cursor to inspect directly (those were
 * the owned box-walker's own bookkeeping); everything here is checked
 * through mp4_open/mp4_seek/mp4_peek_next_kind/mp4_next_sample, the same
 * surface player.cpp itself uses.
 *
 *   g++ -std=c++17 -O2 `pkg-config --cflags libavformat libavcodec \
 *       libavutil libswscale libswresample` -o test_demux \
 *       src/real/test_demux.cpp src/real/mp4_demux.cpp \
 *       `pkg-config --libs libavformat libavcodec libavutil libswscale \
 *       libswresample`
 *   ./test_demux fixture.mp4 audio_fixture.m4a
 */
#include "mp4_demux.h"

#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>

int main(int argc, char** argv) {
    if (argc < 2) { fprintf(stderr, "usage: test_demux fixture.mp4\n"); return 2; }
    wchar_t wpath[2048];
    mbstowcs(wpath, argv[1], 2047);
    wpath[2047] = 0;

    Mp4Demux d;
    int rc = mp4_open(&d, wpath);
    assert(rc == ELY_OK && "fixture must open");

    /* ground truth from make_fixture.py: 10 s, 30 fps video, 44100 stereo
       AAC audio. Exact packet counts are not asserted: a real encoder's
       exact frame/packet count depends on its own priming and padding
       behavior, which is an encoder detail this engine has no say in. */
    assert(d.has_video && d.has_audio);
    assert(d.width > 0 && d.height > 0);
    assert(d.sample_rate == 44100 && d.channels == 2);
    assert(d.duration > 9.5 && d.duration < 10.5);
    assert(d.frame_rate > 29.0 && d.frame_rate < 31.0);
    assert(d.audio.codec_config.size() > 0 && "AAC ASC must be extracted");
    assert(d.video.codec_config.size() > 0 && "avcC must be extracted");
    printf("media info and codec configs: correct (%dx%d, %.2fs, %.1ffps)\n",
          d.width, d.height, d.duration, d.frame_rate);

    /* full interleaved walk: each stream's own pts is non-decreasing (no
       B-frames in this fixture's baseline profile, so decode order equals
       presentation order), sizes are all real and nonzero, first video
       packet is a keyframe. */
    unsigned char buf[1 << 20];
    int kind;
    size_t na = 0, nv = 0;
    double last_a_pts = -1.0, last_v_pts = -1.0;
    int first_video_seen = 0, first_video_keyframe = 0;
    for (;;) {
        size_t size; double pts, dur; int kf;
        if (!mp4_next_sample(&d, &kind, buf, sizeof(buf), &size, &pts, &dur,
                             &kf))
            break;
        assert(size > 0 && "every real sample must carry real bytes");
        if (kind == ELY_MEDIA_VIDEO) {
            assert(pts >= last_v_pts - 1e-6 &&
                  "video pts must be non-decreasing in this baseline-only fixture");
            last_v_pts = pts;
            if (!first_video_seen) {
                first_video_seen = 1;
                first_video_keyframe = kf;
            }
            nv++;
        } else {
            assert(pts >= last_a_pts - 1e-6 && "audio pts must be non-decreasing");
            last_a_pts = pts;
            na++;
        }
    }
    assert(first_video_keyframe && "the first video packet must be a keyframe");
    assert(nv > 250 && nv < 350 && "roughly 300 packets at 30fps for 10s");
    assert(na > 350 && na < 500 && "roughly 430 AAC frames for 10s at 44100");
    printf("interleaved walk: %zu video + %zu audio packets, pts order held\n",
          nv, na);

    /* seek: with this fixture's tight keyframe interval, seeking to 5.05s
       must land the next video packet at or before the target and marked
       as a keyframe. */
    assert(mp4_seek(&d, 5.05));
    size_t size; double pts, dur; int kf;
    assert(mp4_next_sample(&d, &kind, buf, sizeof(buf), &size, &pts, &dur, &kf));
    assert(kind == ELY_MEDIA_VIDEO || kind == ELY_MEDIA_AUDIO);
    if (kind == ELY_MEDIA_VIDEO) {
        assert(kf && "seek must land on or before a sync sample");
        assert(pts <= 5.05 + 1e-3);
    }
    printf("seek snaps to at-or-before the target: correct\n");

    /* damaged container: truncating a real MP4 destroys its moov/mdat
       structure; a real demuxer must report BAD_CONTAINER, not crash and
       not silently succeed. */
    mp4_close(&d);
    {
        FILE* in = fopen(argv[1], "rb");
        const char* trunc_path =
#ifdef _WIN32
            "ely_truncated.mp4";
#else
            "/tmp/ely_truncated.mp4";
#endif
        FILE* out = fopen(trunc_path, "wb");
        assert(in && out);
        unsigned char part[200];
        size_t got = fread(part, 1, sizeof(part), in);
        fwrite(part, 1, got, out);
        fclose(in); fclose(out);
        wchar_t tpath[512];
        mbstowcs(tpath, trunc_path, 511);
        Mp4Demux t;
        rc = mp4_open(&t, tpath);
        assert(rc == ELY_ERR_BAD_CONTAINER &&
              "truncation must be BAD_CONTAINER, not UNSUPPORTED or a crash");
        mp4_close(&t);
    }
    printf("damaged container classified as BAD_CONTAINER\n");

    /* audio-only fixture (second argv): no video track, audio walk intact */
    if (argc > 2) {
        wchar_t apath[2048];
        mbstowcs(apath, argv[2], 2047);
        apath[2047] = 0;
        Mp4Demux a;
        assert(mp4_open(&a, apath) == ELY_OK);
        assert(a.has_audio && !a.has_video);
        assert(a.width == 0 && a.height == 0);
        assert(a.audio.codec_config.size() > 0);
        int akind; size_t an = 0;
        unsigned char abuf[1 << 20];
        for (;;) {
            size_t asize; double apts, adur; int akf;
            if (!mp4_next_sample(&a, &akind, abuf, sizeof(abuf), &asize,
                                 &apts, &adur, &akf))
                break;
            assert(akind == ELY_MEDIA_AUDIO);
            an++;
        }
        assert(an > 0);
        mp4_close(&a);
        printf("audio-only fixture: classification and walk correct (%zu packets)\n",
              an);
    }

    printf("ALL DEMUX TESTS PASSED\n");
    return 0;
}
