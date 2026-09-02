# elysian_video engine contract

This document plus `include/elysian_video.h` is the frozen contract between the Elysian shell and the owned media engine. Implementations change behind it; the contract does not. `test_contract.py` is this document in executable form, and any implementation of the header must pass it.

## Layering

Layer A is the native library (`elysian_video.dll` on Windows, built by `BUILD_DLL.bat`; a `.so` test build exists so the contract can be exercised anywhere). Layer B is `elysian/playback/bindings.py`, pure ctypes, no logic. Layer C is `elysian/playback/video_engine.py`, the shell-facing adapter with the same surface shape and graceful-degradation posture as the audio `PlaybackEngine`. Layer D is the existing shell, untouched until integration.

## ABI stability rules

Enum values are pinned forever and may only be appended. Function signatures never change; new capability arrives as new functions. `ElyMediaInfo` grows only at the end, guarded by the `struct_size` handshake: the caller sets `struct_size = sizeof` before `ely_get_media_info`, the engine copies at most that many bytes, so old callers work against newer engines and vice versa. `ely_abi_version()` returns the generation; bindings refuse a library whose version they do not know. Paths are `wchar_t*` because narrow-char paths through the C runtime are exactly how the shell once lost waveforms on every non-ASCII filename.

## Threading contract

All calls on one `ElyPlayer` handle must be serialized by the caller. The Elysian shell already routes playback through a single worker thread, so this costs nothing. The engine is free to run any internal threads it wants (demux, decode, output, render) provided every public call remains safe under that external serialization and getters never block on pipeline work.

## State machine

States: EMPTY, LOADED, PLAYING, PAUSED, STOPPED, ENDED, ERROR. Legal transitions: EMPTY to LOADED via load; LOADED to PLAYING via play; PLAYING to PAUSED and back via pause/resume; PLAYING or PAUSED to STOPPED via stop; STOPPED to PLAYING via play (from zero); PLAYING to ENDED at natural end of media; ENDED to PLAYING via play (restart from zero); anything to ERROR on fatal failure; ERROR or anything to EMPTY via unload. `play` on EMPTY fails with BAD_STATE. `play` while PLAYING is an idempotent success. `seek` on an ENDED player to a position before the end leaves it PAUSED at that position, never silently playing.

## Semantics that implementations must honor

`load` parses enough to classify the media and fill `ElyMediaInfo`; it starts nothing. `stop` keeps the file loaded, resets position to zero, state STOPPED. `unload` releases everything, state EMPTY. `seek` clamps to `[0, duration]`, and a paused player stays paused at the new position while a playing one keeps playing. `position` derives from the audio clock when audio is active, otherwise from the playback clock, and is zero when EMPTY or STOPPED. `is_finished` is true only at natural end of media with all buffered output drained; demux EOF with queued frames is not finished. `set_volume` clamps to `[0, 1]`. `set_video_hwnd(NULL)` detaches the render target. Every failing call returns a nonzero `ElyResult` and `ely_get_last_error` explains the most recent failure on that handle until the next failing call replaces it.

## Error model

The `ElyResult` values in the header are the complete v1 set: generic, bad argument, file not found, unsupported format, bad container, bad stream, decode failure, audio device failure, video target failure, seek failure, bad state. Fatal worker failures inside the engine promote the player to ERROR; the shell must never crash because a file is bad, which is why Layer C converts every nonzero code into a Python exception carrying the code, its name, and the engine's message.

## The Phase 1 stub

`src/elysian_video_stub.c` implements the complete contract with simulated media: extension-based classification, fixed fake media info, a ten second duration, and a genuinely running monotonic clock, so play advances position, pause freezes it, seek moves it, and the media naturally ENDs. No demuxing, decoding, or output. Its purpose is that the bindings, the wrapper, and later the shell integrate against exact contract behavior before any codec exists.

## Roadmap and the Phase 3 decision

Phases follow the execution plan: contract (done), stub (done), engine infrastructure, MP4 demux, audio decode and output, video decode and render, validation, then shell integration. One decision is deliberately deferred to Phase 3 with eyes open: the code behind this ABI can be hand-rolled demux and codecs, or it can drive the platform decoder (Windows Media Foundation, present on Windows 10/11 with hardware acceleration and no redistributables). Hand-rolling an MP4 demuxer is a reasonable project; hand-rolling H.264 and AAC decoders is a multi-year one. Nothing above this header changes either way, which is the point of freezing it first.

## v1 exclusions

No DRM or encrypted files, no fragmented MP4, no subtitle tracks, no edit-list edge cases, no tolerance guarantees for corrupt files beyond failing with a clean error. Documented here rather than pretended.
