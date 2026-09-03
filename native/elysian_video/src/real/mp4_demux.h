#pragma once
#include "../../include/elysian_video.h"

#include <stdint.h>
#include <vector>

/* MP4-family demuxer, backed by FFmpeg's libavformat.
 *
 * Part E: replaces the hand-rolled ISO-BMFF box walker with
 * avformat_open_input/avformat_find_stream_info. Scope is unchanged from
 * the owned demuxer this replaces: MP4/M4V/MOV, first usable audio track,
 * first usable video track, and only AAC audio / H.264 video are accepted
 * as playable (matching what the decoders behind this ABI understand) -
 * any other codec in an otherwise-valid container is treated the same as
 * an absent track, not a hard failure, so an MP4 with an unsupported audio
 * codec still plays its video.
 *
 * fmt_ctx and pending_pkt are opaque (void*) so this header, and everything
 * that includes it (frame.h's siblings, player.h), stays free of libav*
 * include paths; only mp4_demux.cpp needs them. */

struct Mp4Track {
    int present = 0;
    double duration = 0.0;              /* seconds */
    int width = 0, height = 0;          /* video */
    int sample_rate = 0, channels = 0;   /* audio */
    std::vector<uint8_t> codec_config;   /* extradata: avcC body or AAC ASC */
};

struct Mp4Demux {
    void* fmt_ctx = nullptr;             /* AVFormatContext* */
    void* pending_pkt = nullptr;         /* AVPacket*, one-packet lookahead */
    int pending_kind = 0;                /* which track pending_pkt belongs to */
    int audio_stream_index = -1;
    int video_stream_index = -1;
    double duration;
    double frame_rate;
    int has_audio, has_video;
    int width, height;
    int sample_rate, channels;
    Mp4Track audio;
    Mp4Track video;
};

int mp4_open(Mp4Demux* d, const wchar_t* path);
void mp4_close(Mp4Demux* d);
int mp4_fill_info(Mp4Demux* d, ElyMediaInfo* out);

/* Seeks both streams to the nearest preceding keyframe at or before the
 * target and clears the lookahead packet, so the next read starts clean. */
int mp4_seek(Mp4Demux* d, double seconds);

/* Which track the next packet belongs to, WITHOUT consuming it; returns 0
 * at end of stream. Buffers one packet internally so peeking never costs a
 * read the caller might not want yet. */
int mp4_peek_next_kind(Mp4Demux* d, int* out_kind);

/* Consumes the packet peek_next_kind saw (reading one if none was peeked),
 * copying its encoded bytes into buf. Returns 0 at end of stream or if the
 * packet is larger than buf_cap. out_kind is ELY_MEDIA_AUDIO or
 * ELY_MEDIA_VIDEO; out_pts/out_duration are seconds; out_keyframe is 1 for
 * a sync sample. */
int mp4_next_sample(Mp4Demux* d, int* out_kind, uint8_t* buf, size_t buf_cap,
                    size_t* out_size, double* out_pts, double* out_duration,
                    int* out_keyframe);
