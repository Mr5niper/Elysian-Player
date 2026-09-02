#pragma once
#include "../../include/elysian_video.h"

#include <stdint.h>
#include <stdio.h>
#include <vector>

/* Real ISO-BMFF (MP4/M4A/M4V/MOV) demuxer. Parses the moov box into
 * per-track sample tables and walks compressed samples in decode-time
 * order. No decoding: samples come out exactly as stored, with the codec
 * configuration record (avcC for H.264, AudioSpecificConfig for AAC)
 * extracted for the milestone 2 decoders.
 *
 * v1 scope, per CONTRACT.md: local files, non-fragmented MP4, first audio
 * and first video track, no edit lists, no ctts (pts == dts) yet. */

struct Mp4Sample {
    uint64_t offset;     /* absolute file offset */
    uint32_t size;
    double dts;          /* seconds */
    double duration;     /* seconds */
    int keyframe;
};

struct Mp4Track {
    int present = 0;
    uint32_t timescale = 0;
    double duration = 0.0;           /* seconds */
    int width = 0, height = 0;       /* video */
    int sample_rate = 0, channels = 0;  /* audio */
    std::vector<Mp4Sample> samples;
    std::vector<uint8_t> codec_config;  /* avcC box body or AAC ASC */
    size_t cursor = 0;               /* next sample to hand out */
};

struct Mp4Demux {
    FILE* f;
    double duration;
    double frame_rate;
    int has_audio, has_video;
    int width, height;
    int sample_rate, channels;
    Mp4Track audio;
    Mp4Track video;
};

/* Returns ELY_OK or an ElyResult error. Deviates from the skeleton's bool
 * on purpose: a real parser distinguishes not-an-MP4 (ELY_ERR_UNSUPPORTED)
 * from a damaged MP4 (ELY_ERR_BAD_CONTAINER) from an MP4 with no usable
 * track (ELY_ERR_BAD_STREAM), and the contract tests assert the classes. */
int mp4_open(Mp4Demux* d, const wchar_t* path);
void mp4_close(Mp4Demux* d);
int mp4_fill_info(Mp4Demux* d, ElyMediaInfo* out);

/* Position both track cursors at the given time; the video cursor snaps
 * back to the nearest preceding sync sample so a decoder can start clean. */
int mp4_seek(Mp4Demux* d, double seconds);

/* Which track the next sample belongs to, WITHOUT advancing any cursor;
 * returns 0 at end of media. Lets the pump route to a queue and check its
 * capacity before consuming demux state. */
int mp4_peek_next_kind(Mp4Demux* d, int* out_kind);

/* Next sample across both tracks in dts order; returns 0 at end of media.
 * out_kind is ELY_MEDIA_AUDIO or ELY_MEDIA_VIDEO. The sample's bytes are
 * read into buf (caller-sized); sizes above buf_cap fail with 0. */
int mp4_next_sample(Mp4Demux* d, Mp4Sample* out, int* out_kind,
                    uint8_t* buf, size_t buf_cap);
