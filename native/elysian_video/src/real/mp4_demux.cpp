#include "mp4_demux.h"

#include <stdlib.h>
#include <string.h>
#include <wchar.h>

/* ---- file helpers ------------------------------------------------------- */

static FILE* open_wide(const wchar_t* path) {
#ifdef _WIN32
    return _wfopen(path, L"rb");
#else
    char narrow[4096];
    size_t n = wcstombs(narrow, path, sizeof(narrow) - 1);
    if (n == (size_t)-1) return NULL;
    narrow[n] = 0;
    return fopen(narrow, "rb");
#endif
}

static uint32_t be32(const uint8_t* p) {
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) |
           ((uint32_t)p[2] << 8) | (uint32_t)p[3];
}
static uint64_t be64(const uint8_t* p) {
    return ((uint64_t)be32(p) << 32) | be32(p + 4);
}
static uint16_t be16(const uint8_t* p) {
    return (uint16_t)(((uint16_t)p[0] << 8) | p[1]);
}

/* ---- box cursor over an in-memory buffer -------------------------------- */

struct Box {
    const uint8_t* body;
    size_t size;
};

/* Find the first child box with the given type inside a parent body. */
static int find_box(const uint8_t* body, size_t size, const char* type,
                    Box* out, size_t* offset_after) {
    size_t at = 0;
    while (at + 8 <= size) {
        uint64_t box_size = be32(body + at);
        const uint8_t* t = body + at + 4;
        size_t header = 8;
        if (box_size == 1) {
            if (at + 16 > size) return 0;
            box_size = be64(body + at + 8);
            header = 16;
        } else if (box_size == 0) {
            box_size = size - at;              /* box extends to end */
        }
        if (box_size < header || at + box_size > size) return 0;
        if (memcmp(t, type, 4) == 0) {
            out->body = body + at + header;
            out->size = (size_t)(box_size - header);
            if (offset_after) *offset_after = at + (size_t)box_size;
            return 1;
        }
        at += (size_t)box_size;
    }
    return 0;
}

/* Iterate children: call with *at = 0, returns each box plus its type. */
static int next_box(const uint8_t* body, size_t size, size_t* at,
                    char type[5], Box* out) {
    if (*at + 8 > size) return 0;
    uint64_t box_size = be32(body + *at);
    memcpy(type, body + *at + 4, 4);
    type[4] = 0;
    size_t header = 8;
    if (box_size == 1) {
        if (*at + 16 > size) return 0;
        box_size = be64(body + *at + 8);
        header = 16;
    } else if (box_size == 0) {
        box_size = size - *at;
    }
    if (box_size < header || *at + box_size > size) return 0;
    out->body = body + *at + header;
    out->size = (size_t)(box_size - header);
    *at += (size_t)box_size;
    return 1;
}

/* ---- sample table assembly ---------------------------------------------- */

struct RawTables {
    /* stts */ std::vector<uint32_t> stts_count, stts_delta;
    /* stsz */ uint32_t stsz_uniform = 0; std::vector<uint32_t> stsz;
    /* stsc */ std::vector<uint32_t> stsc_first, stsc_per, stsc_desc;
    /* stco/co64 */ std::vector<uint64_t> chunk_offsets;
    /* stss */ std::vector<uint32_t> sync;    /* 1-based sample numbers */
    int have_stss = 0;
};

static int parse_stbl(const Box& stbl, Mp4Track* trk, RawTables* rt) {
    Box b;
    if (find_box(stbl.body, stbl.size, "stts", &b, NULL)) {
        if (b.size < 8) return 0;
        uint32_t entries = be32(b.body + 4);
        if (b.size < 8 + (size_t)entries * 8) return 0;
        for (uint32_t i = 0; i < entries; i++) {
            rt->stts_count.push_back(be32(b.body + 8 + i * 8));
            rt->stts_delta.push_back(be32(b.body + 12 + i * 8));
        }
    } else return 0;

    if (find_box(stbl.body, stbl.size, "stsz", &b, NULL)) {
        if (b.size < 12) return 0;
        rt->stsz_uniform = be32(b.body + 4);
        uint32_t count = be32(b.body + 8);
        if (rt->stsz_uniform == 0) {
            if (b.size < 12 + (size_t)count * 4) return 0;
            for (uint32_t i = 0; i < count; i++)
                rt->stsz.push_back(be32(b.body + 12 + i * 4));
        } else {
            rt->stsz.assign(count, rt->stsz_uniform);
        }
    } else return 0;

    if (find_box(stbl.body, stbl.size, "stsc", &b, NULL)) {
        if (b.size < 8) return 0;
        uint32_t entries = be32(b.body + 4);
        if (b.size < 8 + (size_t)entries * 12) return 0;
        for (uint32_t i = 0; i < entries; i++) {
            rt->stsc_first.push_back(be32(b.body + 8 + i * 12));
            rt->stsc_per.push_back(be32(b.body + 12 + i * 12));
            rt->stsc_desc.push_back(be32(b.body + 16 + i * 12));
        }
    } else return 0;

    if (find_box(stbl.body, stbl.size, "stco", &b, NULL)) {
        if (b.size < 8) return 0;
        uint32_t entries = be32(b.body + 4);
        if (b.size < 8 + (size_t)entries * 4) return 0;
        for (uint32_t i = 0; i < entries; i++)
            rt->chunk_offsets.push_back(be32(b.body + 8 + i * 4));
    } else if (find_box(stbl.body, stbl.size, "co64", &b, NULL)) {
        if (b.size < 8) return 0;
        uint32_t entries = be32(b.body + 4);
        if (b.size < 8 + (size_t)entries * 8) return 0;
        for (uint32_t i = 0; i < entries; i++)
            rt->chunk_offsets.push_back(be64(b.body + 8 + i * 8));
    } else return 0;

    if (find_box(stbl.body, stbl.size, "stss", &b, NULL)) {
        rt->have_stss = 1;
        if (b.size < 8) return 0;
        uint32_t entries = be32(b.body + 4);
        if (b.size < 8 + (size_t)entries * 4) return 0;
        for (uint32_t i = 0; i < entries; i++)
            rt->sync.push_back(be32(b.body + 8 + i * 4));
    }
    (void)trk;
    return 1;
}

/* Expand the raw tables into a flat, absolute-offset sample list. */
static int build_samples(Mp4Track* trk, const RawTables& rt) {
    size_t total = rt.stsz.size();
    if (!total || rt.chunk_offsets.empty() || rt.stsc_first.empty())
        return 0;

    trk->samples.reserve(total);

    /* chunk mapping: stsc runs apply from first_chunk until the next run */
    size_t sample_index = 0;
    size_t run = 0;
    for (size_t chunk = 1; chunk <= rt.chunk_offsets.size() &&
                           sample_index < total; chunk++) {
        while (run + 1 < rt.stsc_first.size() &&
               chunk >= rt.stsc_first[run + 1])
            run++;
        uint32_t per = rt.stsc_per[run];
        uint64_t offset = rt.chunk_offsets[chunk - 1];
        for (uint32_t s = 0; s < per && sample_index < total; s++) {
            Mp4Sample smp;
            smp.offset = offset;
            smp.size = rt.stsz[sample_index];
            smp.dts = 0.0;
            smp.duration = 0.0;
            smp.keyframe = rt.have_stss ? 0 : 1;
            offset += smp.size;
            trk->samples.push_back(smp);
            sample_index++;
        }
    }
    if (trk->samples.size() != total) return 0;

    /* timestamps from stts */
    uint64_t dts_units = 0;
    size_t at = 0;
    for (size_t e = 0; e < rt.stts_count.size(); e++) {
        for (uint32_t i = 0; i < rt.stts_count[e] && at < total; i++, at++) {
            trk->samples[at].dts = (double)dts_units / trk->timescale;
            trk->samples[at].duration =
                (double)rt.stts_delta[e] / trk->timescale;
            dts_units += rt.stts_delta[e];
        }
    }
    if (at != total) return 0;
    if (trk->duration <= 0.0)
        trk->duration = (double)dts_units / trk->timescale;

    /* keyframes from stss (1-based) */
    for (uint32_t s : rt.sync)
        if (s >= 1 && s <= total) trk->samples[s - 1].keyframe = 1;

    trk->present = 1;
    trk->cursor = 0;
    return 1;
}

/* ---- stsd: codec entry, dimensions, audio params, config record --------- */

static int parse_stsd_video(const Box& stsd, Mp4Track* trk) {
    if (stsd.size < 16) return 0;
    /* full box (4) + entry_count (4) + first entry: size(4) format(4) */
    const uint8_t* entry = stsd.body + 8;
    size_t entry_size = be32(entry);
    if (entry_size < 86 || 8 + entry_size > stsd.size) return 0;
    /* visual sample entry: 8 hdr + 6 reserved + 2 dri + 16 pre_defined
       + width(2) height(2) at offset 32 */
    trk->width = be16(entry + 32);
    trk->height = be16(entry + 34);
    /* avcC lives among the entry's trailing boxes, after the fixed 86 */
    Box cfg;
    if (find_box(entry + 86, entry_size - 86, "avcC", &cfg, NULL)) {
        trk->codec_config.assign(cfg.body, cfg.body + cfg.size);
    }
    return trk->width > 0 && trk->height > 0;
}

static int parse_stsd_audio(const Box& stsd, Mp4Track* trk) {
    if (stsd.size < 16) return 0;
    const uint8_t* entry = stsd.body + 8;
    size_t entry_size = be32(entry);
    if (entry_size < 36 || 8 + entry_size > stsd.size) return 0;
    /* audio sample entry: 8 hdr + 8 reserved + channelcount(2) at 24,
       samplesize(2), pre_defined(2), reserved(2), samplerate(4, 16.16) */
    trk->channels = be16(entry + 24);
    trk->sample_rate = (int)(be32(entry + 32) >> 16);
    /* esds among trailing boxes after the fixed 36 */
    Box esds;
    if (find_box(entry + 36, entry_size - 36, "esds", &esds, NULL)) {
        /* Walk the ES descriptor chain far enough to lift the
           AudioSpecificConfig (tag 0x05). Descriptor lengths are 7-bit
           big-endian varints. */
        const uint8_t* d = esds.body + 4;      /* skip full-box ver/flags */
        size_t left = esds.size > 4 ? esds.size - 4 : 0;
        while (left >= 2) {
            uint8_t tag = d[0];
            size_t len = 0, used = 1;
            while (used < left) {
                uint8_t b = d[used++];
                len = (len << 7) | (b & 0x7F);
                if (!(b & 0x80)) break;
            }
            if (used + len > left) break;
            if (tag == 0x05) {                  /* DecoderSpecificInfo */
                trk->codec_config.assign(d + used, d + used + len);
                break;
            }
            if (tag == 0x03) {                  /* ES_Descriptor: dive in */
                d += used + 3; left -= used + 3; /* skip ES_ID + flags */
                continue;
            }
            if (tag == 0x04) {                  /* DecoderConfig: dive in */
                d += used + 13; left -= used + 13;
                continue;
            }
            d += used + len;                    /* skip unknown sibling */
            left -= used + len;
        }
    }
    return trk->channels > 0 && trk->sample_rate > 0;
}

/* ---- trak --------------------------------------------------------------- */

static void parse_trak(const Box& trak, Mp4Demux* d) {
    Box mdia, mdhd, hdlr, minf, stbl, stsd;
    if (!find_box(trak.body, trak.size, "mdia", &mdia, NULL)) return;
    if (!find_box(mdia.body, mdia.size, "mdhd", &mdhd, NULL)) return;
    if (!find_box(mdia.body, mdia.size, "hdlr", &hdlr, NULL)) return;
    if (!find_box(mdia.body, mdia.size, "minf", &minf, NULL)) return;
    if (!find_box(minf.body, minf.size, "stbl", &stbl, NULL)) return;
    if (!find_box(stbl.body, stbl.size, "stsd", &stsd, NULL)) return;
    if (hdlr.size < 12 || mdhd.size < 20) return;

    Mp4Track trk;
    trk.present = 0;
    trk.width = trk.height = trk.sample_rate = trk.channels = 0;
    trk.duration = 0.0;
    trk.cursor = 0;

    uint8_t version = mdhd.body[0];
    if (version == 1) {
        if (mdhd.size < 32) return;
        trk.timescale = be32(mdhd.body + 20);
        trk.duration = (double)be64(mdhd.body + 24) / trk.timescale;
    } else {
        trk.timescale = be32(mdhd.body + 12);
        trk.duration = (double)be32(mdhd.body + 16) / trk.timescale;
    }
    if (!trk.timescale) return;

    const uint8_t* handler = hdlr.body + 8;
    int is_video = memcmp(handler, "vide", 4) == 0;
    int is_audio = memcmp(handler, "soun", 4) == 0;
    if (!is_video && !is_audio) return;
    if (is_video && d->video.present) return;   /* first track only, v1 */
    if (is_audio && d->audio.present) return;

    if (is_video && !parse_stsd_video(stsd, &trk)) return;
    if (is_audio && !parse_stsd_audio(stsd, &trk)) return;

    RawTables rt;
    if (!parse_stbl(stbl, &trk, &rt)) return;
    if (!build_samples(&trk, rt)) return;

    if (is_video) d->video = trk; else d->audio = trk;
}

/* ---- public ------------------------------------------------------------- */

int mp4_open(Mp4Demux* d, const wchar_t* path) {
    d->audio = Mp4Track();
    d->video = Mp4Track();
    d->f = NULL;
    d->duration = 0.0;
    d->frame_rate = 0.0;
    d->has_audio = d->has_video = 0;
    d->width = d->height = d->sample_rate = d->channels = 0;

    FILE* f = open_wide(path);
    if (!f) return ELY_ERR_NOT_FOUND;

    /* Smell test: the first box must be ftyp (or moov for bare files).
       Anything else is simply not an MP4 -> UNSUPPORTED, not "broken". */
    uint8_t head[12];
    if (fread(head, 1, 12, f) != 12 ||
        (memcmp(head + 4, "ftyp", 4) != 0 &&
         memcmp(head + 4, "moov", 4) != 0)) {
        fclose(f);
        return ELY_ERR_UNSUPPORTED;
    }

    /* Walk top-level boxes to find moov; cap it to keep memory bounded. */
    if (fseek(f, 0, SEEK_SET) != 0) { fclose(f); return ELY_ERR_GENERIC; }
    uint8_t hdr[16];
    long moov_at = -1;
    uint64_t moov_size = 0;
    for (;;) {
        long here = ftell(f);
        if (fread(hdr, 1, 8, f) != 8) break;
        uint64_t box_size = be32(hdr);
        size_t header = 8;
        if (box_size == 1) {
            if (fread(hdr + 8, 1, 8, f) != 8) break;
            box_size = be64(hdr + 8);
            header = 16;
        }
        if (box_size < header) { fclose(f); return ELY_ERR_BAD_CONTAINER; }
        if (memcmp(hdr + 4, "moov", 4) == 0) {
            moov_at = here + (long)header;
            moov_size = box_size - header;
            break;
        }
        if (fseek(f, (long)(box_size - header), SEEK_CUR) != 0) break;
    }
    if (moov_at < 0 || moov_size == 0 || moov_size > (64u << 20)) {
        fclose(f);
        return ELY_ERR_BAD_CONTAINER;
    }

    std::vector<uint8_t> moov((size_t)moov_size);
    if (fseek(f, moov_at, SEEK_SET) != 0 ||
        fread(moov.data(), 1, moov.size(), f) != moov.size()) {
        fclose(f);
        return ELY_ERR_BAD_CONTAINER;
    }

    /* mvhd: presentation timescale and duration */
    Box mvhd;
    if (!find_box(moov.data(), moov.size(), "mvhd", &mvhd, NULL) ||
        mvhd.size < 20) {
        fclose(f);
        return ELY_ERR_BAD_CONTAINER;
    }
    uint8_t version = mvhd.body[0];
    uint32_t ts;
    double dur;
    if (version == 1) {
        if (mvhd.size < 32) { fclose(f); return ELY_ERR_BAD_CONTAINER; }
        ts = be32(mvhd.body + 20);
        dur = ts ? (double)be64(mvhd.body + 24) / ts : 0.0;
    } else {
        ts = be32(mvhd.body + 12);
        dur = ts ? (double)be32(mvhd.body + 16) / ts : 0.0;
    }

    /* every trak */
    size_t at = 0;
    char type[5];
    Box child;
    while (next_box(moov.data(), moov.size(), &at, type, &child))
        if (memcmp(type, "trak", 4) == 0)
            parse_trak(child, d);

    if (!d->audio.present && !d->video.present) {
        fclose(f);
        return ELY_ERR_BAD_STREAM;
    }

    d->f = f;
    d->has_audio = d->audio.present;
    d->has_video = d->video.present;
    d->width = d->video.width;
    d->height = d->video.height;
    d->sample_rate = d->audio.sample_rate;
    d->channels = d->audio.channels;
    d->duration = dur;
    if (d->duration <= 0.0)
        d->duration = d->video.present ? d->video.duration
                                       : d->audio.duration;
    if (d->video.present && d->video.duration > 0.0)
        d->frame_rate = (double)d->video.samples.size() / d->video.duration;
    return ELY_OK;
}

void mp4_close(Mp4Demux* d) {
    if (d->f) fclose(d->f);
    d->f = NULL;
    d->audio.samples.clear();
    d->audio.codec_config.clear();
    d->audio.present = 0;
    d->video.samples.clear();
    d->video.codec_config.clear();
    d->video.present = 0;
    d->duration = 0.0;
    d->has_audio = d->has_video = 0;
}

int mp4_fill_info(Mp4Demux* d, ElyMediaInfo* out) {
    if (!d || !out) return 0;
    out->has_audio = d->has_audio;
    out->has_video = d->has_video;
    out->width = d->width;
    out->height = d->height;
    out->duration = d->duration;
    out->frame_rate = d->frame_rate;
    out->audio_sample_rate = d->sample_rate;
    out->audio_channels = d->channels;
    out->kind = d->has_video ? ELY_MEDIA_VIDEO
              : d->has_audio ? ELY_MEDIA_AUDIO : ELY_MEDIA_UNKNOWN;
    return 1;
}

static size_t lower_bound_dts(const std::vector<Mp4Sample>& v, double t) {
    size_t lo = 0, hi = v.size();
    while (lo < hi) {
        size_t mid = (lo + hi) / 2;
        if (v[mid].dts < t) lo = mid + 1; else hi = mid;
    }
    return lo;
}

int mp4_seek(Mp4Demux* d, double seconds) {
    if (!d || !d->f) return 0;
    if (seconds < 0.0) seconds = 0.0;
    if (d->audio.present)
        d->audio.cursor = lower_bound_dts(d->audio.samples, seconds);
    if (d->video.present) {
        size_t at = lower_bound_dts(d->video.samples, seconds);
        if (at >= d->video.samples.size() && !d->video.samples.empty())
            at = d->video.samples.size() - 1;
        /* snap back to the preceding sync sample so decode starts clean */
        while (at > 0 && !d->video.samples[at].keyframe) at--;
        d->video.cursor = at;
    }
    return 1;
}

int mp4_next_sample(Mp4Demux* d, Mp4Sample* out, int* out_kind,
                    uint8_t* buf, size_t buf_cap) {
    if (!d || !d->f || !out || !out_kind) return 0;
    Mp4Track* a = d->audio.present &&
                  d->audio.cursor < d->audio.samples.size() ? &d->audio : NULL;
    Mp4Track* v = d->video.present &&
                  d->video.cursor < d->video.samples.size() ? &d->video : NULL;
    Mp4Track* pick;
    if (a && v)
        pick = d->audio.samples[d->audio.cursor].dts
             <= d->video.samples[d->video.cursor].dts ? a : v;
    else
        pick = a ? a : v;
    if (!pick) return 0;                        /* end of media */

    const Mp4Sample& s = pick->samples[pick->cursor];
    if (buf) {
        if (s.size > buf_cap) return 0;
        if (fseek(d->f, (long)s.offset, SEEK_SET) != 0) return 0;
        if (fread(buf, 1, s.size, d->f) != s.size) return 0;
    }
    *out = s;
    *out_kind = pick == &d->video ? ELY_MEDIA_VIDEO : ELY_MEDIA_AUDIO;
    pick->cursor++;
    return 1;
}
