/* test_bitstream.cpp - validates the decode seams (aac_decode, h264_decode)
 * directly against real encoded bitstreams, bypassing the full player
 * pipeline. Part E: FFmpeg now owns all bitstream parsing (ASC, avcC,
 * SPS/PPS, slice headers), so this no longer exercises an owned bitreader
 * or owned structural parser - both are gone. What stays true regardless
 * of what sits behind the seam: valid packets must produce real, non-
 * silent/non-flat output, and malformed ones must be rejected rather than
 * crash or silently succeed.
 *
 * Packets are pulled through this engine's own mp4_demux from real_av.mp4,
 * the FFmpeg-muxed fixture already in testdata/, so what reaches the
 * decoders here is exactly what the full pipeline would hand them.
 *
 *   g++ -std=c++17 -O2 `pkg-config --cflags libavformat libavcodec \
 *       libavutil libswscale libswresample` -o test_bitstream \
 *       src/real/test_bitstream.cpp src/real/aac_decode.cpp \
 *       src/real/h264_decode.cpp src/real/mp4_demux.cpp \
 *       `pkg-config --libs libavformat libavcodec libavutil libswscale \
 *       libswresample`
 *   ./test_bitstream testdata/real_asc.bin testdata/real_avcc.bin \
 *       testdata/real_av.mp4
 */
#include "aac_decode.h"
#include "h264_decode.h"
#include "mp4_demux.h"

#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <vector>
#include <wchar.h>

static std::vector<unsigned char> read_file(const char* path) {
    FILE* f = fopen(path, "rb");
    assert(f && "fixture file must exist");
    fseek(f, 0, SEEK_END);
    long n = ftell(f);
    fseek(f, 0, SEEK_SET);
    std::vector<unsigned char> v((size_t)n);
    assert(fread(v.data(), 1, v.size(), f) == v.size());
    fclose(f);
    return v;
}

/* Pulls the first real packet of the requested kind out of a real MP4
 * through this engine's own demuxer, so decode tests exercise exactly what
 * the pipeline would hand them, not a hand-built approximation. */
static bool first_real_packet(const wchar_t* path, int want_kind,
                              std::vector<unsigned char>* out_bytes,
                              double* out_pts, int* out_keyframe) {
    Mp4Demux d;
    if (mp4_open(&d, path) != ELY_OK) return false;
    static unsigned char buf[1 << 20];
    int kind;
    while (mp4_peek_next_kind(&d, &kind)) {
        size_t size; double pts, dur; int kf;
        if (!mp4_next_sample(&d, &kind, buf, sizeof(buf), &size, &pts, &dur,
                             &kf)) {
            break;
        }
        if (kind == want_kind) {
            out_bytes->assign(buf, buf + size);
            *out_pts = pts;
            *out_keyframe = kf;
            mp4_close(&d);
            return true;
        }
    }
    mp4_close(&d);
    return false;
}

int main(int argc, char** argv) {
    if (argc < 4) {
        fprintf(stderr,
               "usage: test_bitstream real_asc.bin real_avcc.bin real_av.mp4\n");
        return 2;
    }

    /* ---- AAC: real ASC parses, real packet decodes non-silent --------- */
    {
        std::vector<unsigned char> asc = read_file(argv[1]);
        AacDecoder d;
        assert(aac_init(&d, asc.data(), asc.size(), 44100, 2) &&
               "the real FFmpeg-encoder ASC must be accepted");
        printf("real encoder ASC (%zu bytes): accepted\n", asc.size());

        wchar_t wpath[2048];
        mbstowcs(wpath, argv[3], 2047);
        wpath[2047] = 0;
        std::vector<unsigned char> pkt_bytes;
        double pts; int kf;
        assert(first_real_packet(wpath, ELY_MEDIA_AUDIO, &pkt_bytes, &pts,
                                 &kf) && "real_av.mp4 must carry audio");

        Packet pkt{};
        pkt.data = pkt_bytes.data();
        pkt.size = pkt_bytes.size();
        pkt.pts = pts;
        PcmFrame out;
        int rc = 0;
        /* A real decoder can legitimately need a few packets before its
         * first frame (encoder priming); feed a handful and require at
         * least one to produce real, non-silent audio. */
        for (int i = 0; i < 8 && rc <= 0; i++) {
            rc = aac_decode_packet(&d, &pkt, &out);
            if (rc <= 0) {
                assert(first_real_packet(wpath, ELY_MEDIA_AUDIO, &pkt_bytes,
                                         &pts, &kf));
                pkt.data = pkt_bytes.data();
                pkt.size = pkt_bytes.size();
                pkt.pts = pts;
            }
        }
        assert(rc > 0 && "a real AAC packet must eventually decode");
        assert(out.frames > 0);
        int nonzero = 0;
        for (float s : out.samples) if (s != 0.0f) { nonzero = 1; break; }
        assert(nonzero && "real AAC decode must not be silent");
        printf("real AAC packet decodes to non-silent PCM (%zu frames)\n",
              out.frames);

        /* malformed: garbage bytes must be rejected, not crash */
        unsigned char garbage[8] = {0xFF, 0x00, 0xDE, 0xAD, 0xBE, 0xEF, 0x00, 0x01};
        Packet bad{};
        bad.data = garbage;
        bad.size = sizeof(garbage);
        PcmFrame bad_out;
        int bad_rc = aac_decode_packet(&d, &bad, &bad_out);
        assert(bad_rc < 0 && "garbage AAC data must be rejected, not accepted");
        printf("malformed AAC packet rejected cleanly\n");
        aac_free(&d);
    }

    /* ---- H.264: real avcC parses, real IDR decodes non-flat ------------ */
    {
        std::vector<unsigned char> avcc = read_file(argv[2]);
        H264Decoder d;
        assert(h264_init(&d, avcc.data(), avcc.size(), 320, 240) &&
               "the real libx264 avcC must be accepted");
        printf("real libx264 avcC (%zu bytes): accepted\n", avcc.size());

        wchar_t wpath[2048];
        mbstowcs(wpath, argv[3], 2047);
        wpath[2047] = 0;
        std::vector<unsigned char> pkt_bytes;
        double pts; int kf;
        assert(first_real_packet(wpath, ELY_MEDIA_VIDEO, &pkt_bytes, &pts,
                                 &kf) && "real_av.mp4 must carry video");
        assert(kf && "the first video packet in a file must be a keyframe");

        Packet pkt{};
        pkt.data = pkt_bytes.data();
        pkt.size = pkt_bytes.size();
        pkt.pts = pts;
        pkt.keyframe = kf;
        VideoFrame out;
        assert(h264_decode_packet(&d, &pkt, &out) > 0 &&
              "a real IDR packet must decode on the first call");
        assert(out.width == 320 && out.height == 240);
        assert(!out.pixels.empty());
        unsigned char first = out.pixels[0];
        int all_same = 1;
        for (unsigned char b : out.pixels) if (b != first) { all_same = 0; break; }
        assert(!all_same && "real decoded video must not be a flat fill");
        printf("real H.264 IDR decodes to non-flat %dx%d pixels\n",
              out.width, out.height);

        /* malformed: garbage NAL bytes must be rejected, not crash */
        unsigned char garbage[6] = {0x65, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
        Packet bad{};
        bad.data = garbage;
        bad.size = sizeof(garbage);
        VideoFrame bad_out;
        int bad_rc = h264_decode_packet(&d, &bad, &bad_out);
        assert(bad_rc < 0 && "garbage H.264 data must be rejected, not accepted");
        printf("malformed H.264 packet rejected cleanly\n");
        h264_free(&d);
    }

    printf("ALL BITSTREAM TESTS PASSED\n");
    return 0;
}
