/* test_demux.cpp - ground-truth checks on the demuxer internals that the
 * ABI does not expose: sample counts, interleave order, sizes, keyframe
 * marking, sync-snap seeking and codec-config extraction. Run against a
 * fixture from make_fixture.py:
 *
 *   Linux:   g++ -std=c++17 -O2 -o test_demux src/real/test_demux.cpp \
 *                src/real/mp4_demux.cpp && ./test_demux fixture.mp4
 *   Windows: cl /EHsc /std:c++17 src\real\test_demux.cpp \
 *                src\real\mp4_demux.cpp && test_demux fixture.mp4
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

    /* ground truth from make_fixture.py: 10 s, 30 fps video, 1024-sample
       AAC frames at 44100, sizes 100 and 50, sync every 30th frame */
    assert(d.has_video && d.has_audio);
    assert(d.width == 1920 && d.height == 1080);
    assert(d.sample_rate == 44100 && d.channels == 2);
    assert(d.duration > 9.99 && d.duration < 10.01);
    assert(d.frame_rate > 29.9 && d.frame_rate < 30.1);
    assert(d.video.samples.size() == 300);
    assert(d.audio.samples.size() == 430);
    assert(d.video.codec_config.size() > 0 && "avcC must be extracted");
    assert(d.audio.codec_config.size() == 2 && "AAC ASC must be extracted");
    assert(d.audio.codec_config[0] == 0x12 && d.audio.codec_config[1] == 0x10);
    printf("media info, tables and codec configs: correct\n");

    /* keyframes: 1, 31, 61 ... marked, others not */
    assert(d.video.samples[0].keyframe == 1);
    assert(d.video.samples[1].keyframe == 0);
    assert(d.video.samples[30].keyframe == 1);
    printf("sync-sample marking: correct\n");

    /* full interleaved walk: monotonic dts, exact totals, sizes right */
    unsigned char buf[4096];
    Mp4Sample s; int kind;
    size_t na = 0, nv = 0;
    double last_dts = -1.0;
    while (mp4_next_sample(&d, &s, &kind, buf, sizeof(buf))) {
        assert(s.dts >= last_dts - 1e-9 && "dts order must be monotonic");
        last_dts = s.dts;
        if (kind == ELY_MEDIA_VIDEO) { assert(s.size == 100); nv++; }
        else { assert(s.size == 50); na++; }
    }
    assert(nv == 300 && na == 430);
    printf("interleaved walk: %zu video + %zu audio samples in order\n", nv, na);

    /* seek: video cursor snaps back to the preceding sync sample */
    assert(mp4_seek(&d, 5.05));
    assert(mp4_next_sample(&d, &s, &kind, buf, sizeof(buf)));
    /* first sample after a 5.05 s seek must be a video keyframe at 5.0 or
       the audio sample right at the target, whichever sorts first; assert
       the video track cursor specifically */
    assert(d.video.samples[d.video.cursor > 0 ? d.video.cursor - 1
                                              : 0].keyframe == 1
           || d.video.samples[d.video.cursor].keyframe == 1);
    assert(mp4_seek(&d, 5.05));
    size_t vc = d.video.cursor;
    assert(d.video.samples[vc].keyframe == 1 && "seek must land on sync");
    assert(d.video.samples[vc].dts <= 5.05);
    printf("seek snaps to the preceding sync sample: correct\n");

    /* damaged container: ftyp present but moov truncated away must be
       BAD_CONTAINER, not UNSUPPORTED and not a crash */
    mp4_close(&d);
    {
        FILE* in = fopen(argv[1], "rb");
        FILE* out = fopen("/tmp/ely_truncated.mp4", "wb");
#ifdef _WIN32
        out = fopen("ely_truncated.mp4", "wb");
#endif
        assert(in && out);
        unsigned char part[100];
        size_t got = fread(part, 1, sizeof(part), in);
        fwrite(part, 1, got, out);
        fclose(in); fclose(out);
        Mp4Demux t;
#ifdef _WIN32
        rc = mp4_open(&t, L"ely_truncated.mp4");
#else
        rc = mp4_open(&t, L"/tmp/ely_truncated.mp4");
#endif
        assert(rc == ELY_ERR_BAD_CONTAINER && "truncation is BAD_CONTAINER");
        mp4_close(&t);
    }
    printf("damaged container classified as BAD_CONTAINER\n");
    printf("ALL DEMUX TESTS PASSED\n");
    return 0;
}
