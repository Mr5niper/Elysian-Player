/* test_bitstream.cpp - validates the C.3 bitstream foundations: the shared
 * bitreader, ASC parsing in the AAC seam, and avcC/SPS/PPS parsing in the
 * H.264 seam. Ground truth comes from three directions: hand-computed
 * values, the fixture generator's spec-valid bitstreams, and REAL encoder
 * output (FFmpeg's AAC encoder and libx264) stored in testdata/.
 *
 *   g++ -std=c++17 -O2 -o test_bitstream src/real/test_bitstream.cpp \
 *       src/real/aac_decode.cpp src/real/h264_decode.cpp
 *   ./test_bitstream testdata/real_asc.bin testdata/real_avcc.bin
 */
#include "bitreader.h"
#include "aac_decode.h"
#include "h264_decode.h"

#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <vector>

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

int main(int argc, char** argv) {
    /* ---- bitreader: exact reads, Exp-Golomb, sticky exhaustion ---------- */
    {
        const uint8_t data[] = {0b10110100, 0b01100000};
        BitReader br;
        br_init(&br, data, 2);
        assert(br_read1(&br) == 1);
        assert(br_read(&br, 3) == 0b011);
        assert(br_read(&br, 4) == 0b0100);
        assert(br_read(&br, 3) == 0b011);
        assert(br_ok(&br));
        br_read(&br, 6);                       /* exhausts (5 left) */
        assert(!br_ok(&br));
        assert(br_read1(&br) == 0 && !br_ok(&br));  /* stays failed */
    }
    {
        /* ue(v): 1 -> 0, 010 -> 1, 011 -> 2, 00100 -> 3; se follows */
        const uint8_t data[] = {0b10100110, 0b01000000};
        BitReader br;
        br_init(&br, data, 2);
        assert(br_ue(&br) == 0);
        assert(br_ue(&br) == 1);
        assert(br_ue(&br) == 2);
        assert(br_ue(&br) == 3);
        assert(br_ok(&br));
    }
    {
        const uint8_t data[] = {0b01001110};   /* ue=1 -> se=1; ue=2 -> se=-1 */
        BitReader br;
        br_init(&br, data, 1);
        assert(br_se(&br) == 1);
        assert(br_se(&br) == -1);
        assert(br_ok(&br));
    }
    printf("bitreader: reads, exp-golomb and exhaustion correct\n");

    /* ---- AAC ASC: fixture-style, escape form, malformed ----------------- */
    {
        AacDecoder d;
        const unsigned char asc[] = {0x12, 0x10};   /* LC, 44100, stereo */
        assert(aac_init(&d, asc, 2, 44100, 2));
        assert(d.asc_object_type == 2 && d.asc_rate == 44100 &&
               d.asc_channel_config == 2);
        aac_free(&d);

        /* escape form: rate index 15, explicit 24-bit rate */
        /* 5 bits type=2, 4 bits idx=15, 24 bits rate=44100, 4 bits ch=1 */
        AacDecoder e;
        {
            uint64_t bits = 0;
            int n = 0;
            auto put = [&](uint32_t v, int c) {
                bits = (bits << c) | v;
                n += c;
            };
            put(2, 5); put(15, 4); put(44100, 24); put(1, 4);
            put(0, (8 - (n % 8)) % 8);
            n += (8 - (n % 8)) % 8;
            unsigned char buf[8];
            for (int i = 0; i < n / 8; i++)
                buf[i] = (unsigned char)(bits >> (n - 8 * (i + 1)));
            assert(aac_init(&e, buf, (size_t)(n / 8), 44100, 1));
            assert(e.asc_rate == 44100 && e.asc_rate_index == 15);
            aac_free(&e);
        }

        /* rejections: HE-AAC object type, rate mismatch, truncation */
        AacDecoder r;
        const unsigned char he[] = {0x2B, 0x10};    /* object type 5 */
        assert(!aac_init(&r, he, 2, 44100, 2));
        assert(!aac_init(&r, asc, 2, 48000, 2));    /* container disagrees */
        assert(!aac_init(&r, asc, 1, 44100, 2));    /* truncated */
        printf("AAC ASC: parse, escape form and rejection correct\n");
    }

    /* ---- AAC packet path: valid packet => non-silent PCM, malformed fails --- */
    {
        AacDecoder d;
        const unsigned char asc[] = {0x12, 0x10};   /* LC, 44100, stereo */
        assert(aac_init(&d, asc, 2, 44100, 2));

        /* Minimal supported CPE-ish packet: element_id=1, tag=0, common_window=0,
           then enough zero bits for two ICS parses to succeed structurally. */
        const unsigned char pkt_bytes[] = { 0x20, 0x00, 0x00, 0x00 };
        Packet pkt{};
        pkt.data = (unsigned char*)pkt_bytes;
        pkt.size = sizeof(pkt_bytes);
        pkt.pts = 1.25;

        PcmFrame out;
        assert(aac_decode_packet(&d, &pkt, &out));
        assert(out.frames == 1024);
        assert(out.channels == 2);
        int nonzero = 0;
        for (float s : out.samples) {
            if (s != 0.0f) { nonzero = 1; break; }
        }
        assert(nonzero && "AAC output must no longer be silent");
        aac_free(&d);

        Packet bad{};
        bad.data = (unsigned char*)pkt_bytes;
        bad.size = 0;
        PcmFrame bad_out;
        assert(!aac_decode_packet(&d, &bad, &bad_out));
        printf("AAC packet path: non-silent valid output, malformed rejects\n");
    }


    /* ---- real encoder ASC ------------------------------------------------ */
    if (argc > 1) {
        std::vector<unsigned char> asc = read_file(argv[1]);
        AacDecoder d;
        assert(aac_init(&d, asc.data(), asc.size(), 44100, 2) &&
               "the real FFmpeg-encoder ASC must parse");
        assert(d.asc_object_type == 2 && d.asc_rate == 44100 &&
               d.asc_channel_config == 2);
        aac_free(&d);
        printf("real encoder ASC (%zu bytes): parsed as LC/44100/stereo\n",
               asc.size());
    }

    /* ---- real libx264 avcC ------------------------------------------------ */
    if (argc > 2) {
        std::vector<unsigned char> avcc = read_file(argv[2]);
        H264Decoder d;
        assert(h264_init(&d, avcc.data(), avcc.size(), 320, 240) &&
               "the real libx264 avcC must parse");
        assert(d.have_sps && d.have_pps);
        assert(d.sps.width == 320 && d.sps.height == 240);
        assert(d.sps.profile_idc == 66 || d.sps.profile_idc == 77 ||
               d.sps.profile_idc == 100);
        assert(d.nal_length_size == 4);
        printf("real libx264 avcC: SPS %dx%d profile %d, PPS %s\n",
               d.sps.width, d.sps.height, d.sps.profile_idc,
               d.pps.entropy_cabac ? "CABAC" : "CAVLC");

        /* container/bitstream disagreement must be rejected */
        H264Decoder bad;
        assert(!h264_init(&bad, avcc.data(), avcc.size(), 640, 480));

        /* malformed: truncated record, wrong version */
        H264Decoder t;
        assert(!h264_init(&t, avcc.data(), 6, 320, 240));
        std::vector<unsigned char> wrong = avcc;
        wrong[0] = 2;
        assert(!h264_init(&t, wrong.data(), wrong.size(), 320, 240));
        printf("avcC rejection: dimension mismatch, truncation, bad version\n");
        h264_free(&d);
    }

    /* ---- H.264 packet path: structured output, malformed fails -------------- */
    if (argc > 2) {
        std::vector<unsigned char> avcc = read_file(argv[2]);
        H264Decoder d;
        assert(h264_init(&d, avcc.data(), avcc.size(), 320, 240));

        /* one IDR slice NAL, MP4 length-prefixed */
        std::vector<unsigned char> au = {
            0x00, 0x00, 0x00, 0x04,   /* nal len */
            0x65, 0xB8, 0x00, 0x00    /* tiny RBSP-shaped slice */
        };
        Packet pkt{};
        pkt.data = au.data();
        pkt.size = au.size();
        pkt.pts = 0.5;
        pkt.keyframe = 1;

        VideoFrame out;
        assert(h264_decode_packet(&d, &pkt, &out));
        assert(out.width == 320 && out.height == 240);
        assert(!out.pixels.empty());

        unsigned char first = out.pixels[0];
        int all_same = 1;
        for (unsigned char b : out.pixels) {
            if (b != first) { all_same = 0; break; }
        }
        assert(!all_same && "H.264 output must no longer be a flat tint fill");

        std::vector<unsigned char> bad = { 0x00, 0x00, 0x00, 0x20, 0x65 };
        Packet badpkt{};
        badpkt.data = bad.data();
        badpkt.size = bad.size();
        VideoFrame badout;
        assert(!h264_decode_packet(&d, &badpkt, &badout));
        h264_free(&d);
        printf("H.264 packet path: structured output, malformed rejects\n");
    }


    printf("ALL BITSTREAM TESTS PASSED\n");
    return 0;
}
