#pragma once
#include <stddef.h>
#include <stdint.h>

/* MSB-first bitstream reader shared by the AAC and H.264 parsing layers.
 * Reads never run past the buffer: exhaustion sets a sticky error flag and
 * returns zeros, so parsers check br_ok() once at the end instead of after
 * every field. Header-only; no allocation. */
struct BitReader {
    const uint8_t* data;
    size_t size;
    size_t bit;         /* next bit index */
    int error;
};

static inline void br_init(BitReader* b, const uint8_t* data, size_t size) {
    b->data = data;
    b->size = size;
    b->bit = 0;
    b->error = 0;
}

static inline int br_ok(const BitReader* b) { return !b->error; }

static inline size_t br_bits_left(const BitReader* b) {
    return b->size * 8 - b->bit;
}

static inline uint32_t br_read(BitReader* b, int count) {
    uint32_t v = 0;
    if (count < 0 || count > 32) { b->error = 1; return 0; }
    for (int i = 0; i < count; i++) {
        if (b->bit >= b->size * 8) { b->error = 1; return 0; }
        size_t byte = b->bit >> 3;
        int shift = 7 - (int)(b->bit & 7);
        v = (v << 1) | ((b->data[byte] >> shift) & 1);
        b->bit++;
    }
    return v;
}

static inline uint32_t br_read1(BitReader* b) { return br_read(b, 1); }

/* Exp-Golomb, unsigned (H.264 ue(v)). */
static inline uint32_t br_ue(BitReader* b) {
    int zeros = 0;
    while (br_ok(b) && br_read1(b) == 0) {
        zeros++;
        if (zeros > 31) { b->error = 1; return 0; }
    }
    if (!br_ok(b)) return 0;
    uint32_t suffix = zeros ? br_read(b, zeros) : 0;
    return ((1u << zeros) - 1) + suffix;
}

/* Exp-Golomb, signed (H.264 se(v)). */
static inline int32_t br_se(BitReader* b) {
    uint32_t ue = br_ue(b);
    int32_t k = (int32_t)((ue + 1) / 2);
    return (ue & 1) ? k : -k;
}
