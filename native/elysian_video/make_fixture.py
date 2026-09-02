"""Builds small, structurally valid MP4 fixtures for the contract tests.

A real demuxer cannot be exercised by one-byte fake files, so the suite
writes actual ISO-BMFF: ftyp, mdat (payload first, so chunk offsets are
known before moov is assembled), then a moov with correct mvhd, tkhd, mdhd,
hdlr, dinf, stsd (avc1+avcC / mp4a+esds), stts, stss, stsc, stsz and stco.
The payload bytes are zeros; nothing here decodes them. Ten seconds long,
1920x1080 at 30 fps, AAC-LC shaped audio entry at 44100 stereo, matching
the values the stub fakes so both implementations pass one suite.
"""
import struct


def box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + kind + payload


def full(kind: bytes, version: int, flags: int, payload: bytes) -> bytes:
    return box(kind, struct.pack(">B", version)
               + struct.pack(">I", flags)[1:] + payload)


def _matrix() -> bytes:
    return struct.pack(">9i", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)


def _tkhd(track_id: int, duration_mv: int, w: int, h: int) -> bytes:
    body = struct.pack(">II", 0, 0)                  # ctime, mtime
    body += struct.pack(">I", track_id)
    body += struct.pack(">I", 0)                     # reserved
    body += struct.pack(">I", duration_mv)
    body += b"\x00" * 8                              # reserved
    body += struct.pack(">hhhh", 0, 0, 0x0100, 0)    # layer, group, vol, res
    body += _matrix()
    body += struct.pack(">II", w << 16, h << 16)
    return full(b"tkhd", 0, 7, body)


def _mdhd(timescale: int, duration: int) -> bytes:
    body = struct.pack(">IIII", 0, 0, timescale, duration)
    body += struct.pack(">HH", 0x55C4, 0)            # language "und"
    return full(b"mdhd", 0, 0, body)


def _hdlr(handler: bytes) -> bytes:
    body = struct.pack(">I", 0) + handler + b"\x00" * 12 + b"\x00"
    return full(b"hdlr", 0, 0, body)


def _dinf() -> bytes:
    url = full(b"url ", 0, 1, b"")
    dref = full(b"dref", 0, 0, struct.pack(">I", 1) + url)
    return box(b"dinf", dref)


def _avc1(w: int, h: int) -> bytes:
    entry = b"\x00" * 6 + struct.pack(">H", 1)       # reserved + dri
    entry += struct.pack(">HH", 0, 0) + b"\x00" * 12  # pre_defined/reserved
    entry += struct.pack(">HH", w, h)
    entry += struct.pack(">II", 0x00480000, 0x00480000)  # 72 dpi
    entry += struct.pack(">I", 0)                    # reserved
    entry += struct.pack(">H", 1)                    # frame_count
    entry += b"\x00" * 32                            # compressorname
    entry += struct.pack(">Hh", 0x0018, -1)          # depth, pre_defined
    entry += box(b"avcC", _avcc(w, h))
    return box(b"avc1", entry)


def _esds() -> bytes:
    asc = bytes([0x12, 0x10])                        # AAC-LC, 44100, stereo
    tag5 = bytes([0x05, len(asc)]) + asc
    tag4_body = bytes([0x40, 0x15]) + b"\x00\x00\x00" + b"\x00" * 8 + tag5
    tag4 = bytes([0x04, len(tag4_body)]) + tag4_body
    tag6 = bytes([0x06, 0x01, 0x02])
    tag3_body = struct.pack(">HB", 1, 0) + tag4 + tag6
    tag3 = bytes([0x03, len(tag3_body)]) + tag3_body
    return full(b"esds", 0, 0, tag3)


def _mp4a(sample_rate: int, channels: int) -> bytes:
    entry = b"\x00" * 6 + struct.pack(">H", 1)       # reserved + dri
    entry += b"\x00" * 8                             # reserved
    entry += struct.pack(">HHHH", channels, 16, 0, 0)
    entry += struct.pack(">I", sample_rate << 16)
    entry += _esds()
    return box(b"mp4a", entry)


def _stbl(entry: bytes, count: int, delta: int, size: int,
          chunk_offset: int, sync_every: int | None) -> bytes:
    stsd = full(b"stsd", 0, 0, struct.pack(">I", 1) + entry)
    stts = full(b"stts", 0, 0, struct.pack(">III", 1, count, delta))
    stsc = full(b"stsc", 0, 0, struct.pack(">IIII", 1, 1, count, 1))
    stsz = full(b"stsz", 0, 0, struct.pack(">III", size, count, 0)[:8]
                + struct.pack(">I", count))
    stco = full(b"stco", 0, 0, struct.pack(">II", 1, chunk_offset))
    out = stsd + stts
    if sync_every:
        syncs = list(range(1, count + 1, sync_every))
        stss = full(b"stss", 0, 0, struct.pack(">I", len(syncs))
                    + b"".join(struct.pack(">I", s) for s in syncs))
        out += stss
    return box(b"stbl", out + stsc + stsz + stco)


def _trak(track_id: int, mv_duration: int, handler: bytes, timescale: int,
          duration: int, entry: bytes, count: int, delta: int, size: int,
          chunk_offset: int, w: int = 0, h: int = 0,
          sync_every: int | None = None) -> bytes:
    mhd = (full(b"vmhd", 0, 1, struct.pack(">HHHH", 0, 0, 0, 0))
           if handler == b"vide"
           else full(b"smhd", 0, 0, struct.pack(">HH", 0, 0)))
    stbl = _stbl(entry, count, delta, size, chunk_offset, sync_every)
    minf = box(b"minf", mhd + _dinf() + stbl)
    mdia = box(b"mdia", _mdhd(timescale, duration) + _hdlr(handler) + minf)
    return box(b"trak", _tkhd(track_id, mv_duration, w, h) + mdia)


def write_mp4(path, video: bool = True, seconds: float = 10.0,
              width: int = 1920, height: int = 1080) -> None:
    """Write a valid MP4 (or audio-only M4A shape) of the given length."""
    mv_timescale = 1000
    mv_duration = int(seconds * mv_timescale)

    vid_count, vid_size = int(seconds * 30), 100     # 30 fps
    aud_count, aud_size = int(seconds * 44100 / 1024), 50

    ftyp = box(b"ftyp", b"isom" + struct.pack(">I", 0) + b"isomiso2")
    mdat_payload_size = (vid_count * vid_size if video else 0) \
        + aud_count * aud_size
    mdat_at = len(ftyp)
    data_at = mdat_at + 8
    vid_chunk = data_at
    aud_chunk = data_at + (vid_count * vid_size if video else 0)

    traks = b""
    tid = 1
    if video:
        traks += _trak(tid, mv_duration, b"vide", 30, vid_count,
                       _avc1(width, height), vid_count, 1, vid_size,
                       vid_chunk, w=width, h=height, sync_every=30)
        tid += 1
    traks += _trak(tid, mv_duration, b"soun", 44100, aud_count * 1024,
                   _mp4a(44100, 2), aud_count, 1024, aud_size, aud_chunk)

    mvhd_body = struct.pack(">IIII", 0, 0, mv_timescale, mv_duration)
    mvhd_body += struct.pack(">IH", 0x00010000, 0x0100) + b"\x00" * 10
    mvhd_body += _matrix() + b"\x00" * 24 + struct.pack(">I", tid + 1)
    moov = box(b"moov", full(b"mvhd", 0, 0, mvhd_body) + traks)

    # Video samples are structurally valid MP4-form access units: a 4-byte
    # NAL length prefix, a NAL header, then a REAL slice header assembled
    # for this fixture's own SPS (log2_max_frame_num 4, poc_type 0,
    # log2_max_poc_lsb 4), then padding. The H.264 seam parses the slice
    # header for real now; zero-filled payloads would rightly be rejected.
    payload = bytearray()
    if video:
        for i in range(vid_count):
            idr = i % 30 == 0
            payload += struct.pack(">I", vid_size - 4)
            payload += _slice_nal(idr, frame_num=i % 16,
                                  poc_lsb=(2 * i) % 16,
                                  pad_to=vid_size - 4)
    payload += b"\x00" * (aud_count * aud_size)
    assert len(payload) == mdat_payload_size

    with open(path, "wb") as fh:
        fh.write(ftyp)
        fh.write(box(b"mdat", bytes(payload)))
        fh.write(moov)


class _BitWriter:
    """MSB-first bit writer for assembling real SPS/PPS bitstreams."""

    def __init__(self):
        self.bits = []

    def u(self, value: int, count: int) -> None:
        for i in range(count - 1, -1, -1):
            self.bits.append((value >> i) & 1)

    def ue(self, value: int) -> None:
        v = value + 1
        zeros = v.bit_length() - 1
        self.u(0, zeros)
        self.u(v, zeros + 1)

    def se(self, value: int) -> None:
        self.ue(2 * value - 1 if value > 0 else -2 * value)

    def trailing(self) -> None:
        self.bits.append(1)
        while len(self.bits) % 8:
            self.bits.append(0)

    def bytes(self) -> bytes:
        out = bytearray()
        for i in range(0, len(self.bits), 8):
            b = 0
            for bit in self.bits[i:i + 8]:
                b = (b << 1) | bit
            out.append(b)
        return bytes(out)


def _escape_rbsp(rbsp: bytes) -> bytes:
    out = bytearray()
    zeros = 0
    for byte in rbsp:
        if zeros >= 2 and byte <= 3:
            out.append(3)
            zeros = 0
        out.append(byte)
        zeros = zeros + 1 if byte == 0 else 0
    return bytes(out)


def _sps(w: int, h: int) -> bytes:
    """A spec-valid baseline SPS for WxH (4:2:0, frames only)."""
    mb_w = (w + 15) // 16
    mb_h = (h + 15) // 16
    crop_r = (mb_w * 16 - w) // 2
    crop_b = (mb_h * 16 - h) // 2
    bw = _BitWriter()
    bw.u(66, 8)          # profile_idc: baseline
    bw.u(0xC0, 8)        # constraint flags
    bw.u(30, 8)          # level 3.0
    bw.ue(0)             # sps_id
    bw.ue(0)             # log2_max_frame_num_minus4
    bw.ue(0)             # poc_type 0
    bw.ue(0)             # log2_max_poc_lsb_minus4
    bw.ue(1)             # max_num_ref_frames
    bw.u(0, 1)           # gaps_in_frame_num_allowed
    bw.ue(mb_w - 1)
    bw.ue(mb_h - 1)
    bw.u(1, 1)           # frame_mbs_only
    bw.u(0, 1)           # direct_8x8
    if crop_r or crop_b:
        bw.u(1, 1)
        bw.ue(0); bw.ue(crop_r); bw.ue(0); bw.ue(crop_b)
    else:
        bw.u(0, 1)
    bw.u(0, 1)           # vui absent
    bw.trailing()
    return bytes([0x67]) + _escape_rbsp(bw.bytes())


def _pps() -> bytes:
    bw = _BitWriter()
    bw.ue(0)             # pps_id
    bw.ue(0)             # sps_id
    bw.u(0, 1)           # entropy: CAVLC
    bw.u(0, 1)           # bottom_field_pic_order
    bw.ue(0)             # one slice group
    bw.ue(0)             # refs l0
    bw.ue(0)             # refs l1
    bw.u(0, 1)           # weighted pred
    bw.u(0, 2)           # weighted bipred
    bw.se(0)             # pic_init_qp_minus26
    bw.se(0)             # pic_init_qs_minus26
    bw.se(0)             # chroma_qp_offset
    bw.u(1, 1)           # deblocking_filter_control
    bw.u(0, 1)           # constrained_intra
    bw.u(0, 1)           # redundant_pic_cnt
    bw.trailing()
    return bytes([0x68]) + _escape_rbsp(bw.bytes())


def _avcc(w: int, h: int) -> bytes:
    sps = _sps(w, h)
    pps = _pps()
    out = bytes([1, sps[1], sps[2], sps[3], 0xFF, 0xE1])
    out += struct.pack(">H", len(sps)) + sps
    out += bytes([1]) + struct.pack(">H", len(pps)) + pps
    return out


def _slice_nal(idr: bool, frame_num: int, poc_lsb: int, pad_to: int) -> bytes:
    """A NAL with a real slice header for the fixture SPS, zero-padded."""
    bw = _BitWriter()
    bw.ue(0)                     # first_mb_in_slice
    bw.ue(2 if idr else 0)       # slice_type: I or P
    bw.ue(0)                     # pps_id
    bw.u(frame_num, 4)           # frame_num (log2_max_frame_num = 4)
    bw.u(poc_lsb, 4)             # pic_order_cnt_lsb (log2 = 4)
    if idr:
        bw.ue(0)                 # idr_pic_id
    bw.trailing()
    nal = bytes([0x65 if idr else 0x41]) + _escape_rbsp(bw.bytes())
    assert len(nal) <= pad_to
    return nal + b"\x00" * (pad_to - len(nal))
