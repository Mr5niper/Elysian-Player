# elysian_video engine contract

This document plus `include/elysian_video.h` is the frozen contract between the Elysian shell and the native media engine. Implementations change behind it; the contract does not. `test_contract.py` is this document in executable form, and any implementation of the header must pass it.

## Layering

Layer A is the native library (`elysian_video.dll` on Windows, built by `BUILD_DLL.bat`; a `.so` test build exists so the contract can be exercised anywhere). Layer B is `elysian/playback/bindings.py`, pure ctypes, no logic. Layer C is `elysian/playback/video_engine.py`, the shell-facing adapter with the same surface shape and graceful-degradation posture as the audio `PlaybackEngine`. Layer D is the existing shell, untouched until integration.

## ABI stability rules

Enum values are pinned forever and may only be appended. Function signatures never change; new capability arrives as new functions. `ElyMediaInfo` grows only at the end, guarded by the `struct_size` handshake: the caller sets `struct_size = sizeof` before `ely_get_media_info`, the engine copies at most that many bytes, so old callers work against newer engines and vice versa. `ely_abi_version()` returns the generation; bindings refuse a library whose version they do not know. Paths are `wchar_t*` because narrow-char paths through the C runtime are exactly how the shell once lost waveforms on every non-ASCII filename.

## Threading contract

All calls on one `ElyPlayer` handle must be serialized by the caller. The Elysian shell already routes playback through a single worker thread, so this costs nothing. The engine is free to run any internal threads it wants (demux, decode, output, render) with two hard rules: every getter is non-blocking under that external serialization and never waits on pipeline work, and the engine never calls back into caller code from its own threads. State is pulled through getters, never pushed through callbacks; that rule is what keeps the shell's snapshot model intact.

## Locked v1 policy decisions

These were open questions; they are now decided and Part B may not reopen them. Capability queries live in `ElyMediaInfo` only; there are no separate `has_audio`/`has_video`/`kind` functions because the struct with its `struct_size` handshake already answers them in one call. Textual metadata (title, artist, album) is out of the ABI: the shell owns display metadata, exactly as it already does for audio with mutagen and filename fallback, and a future need for container-title text arrives as new functions, not a v1 change. `ely_get_last_error` describes the most recent failure on the handle; success never clears it, the next failure replaces it, and it is the empty string until the first failure. `ely_resize_video` with no attached target is a legal no-op success so the shell can report layout changes without tracking attachment. Paths are local filesystem paths only: no URLs, no device paths, no pipes or streams in v1.

## State machine

States: EMPTY, LOADED, PLAYING, PAUSED, STOPPED, ENDED, ERROR. Legal transitions: EMPTY to LOADED via load; LOADED to PLAYING via play; PLAYING to PAUSED and back via pause/resume; PLAYING or PAUSED to STOPPED via stop; STOPPED to PLAYING via play (from zero); PLAYING to ENDED at natural end of media; ENDED to PLAYING via play (restart from zero); anything to ERROR on fatal failure; ERROR or anything to EMPTY via unload. `play` on EMPTY fails with BAD_STATE. `play` while PLAYING is an idempotent success. `seek` on an ENDED player to a position before the end leaves it PAUSED at that position, never silently playing.

## Semantics that implementations must honor

`load` parses enough to classify the media and fill `ElyMediaInfo`; it starts nothing. `stop` keeps the file loaded, resets position to zero, state STOPPED. `unload` releases everything, state EMPTY. `seek` clamps to `[0, duration]`, and a paused player stays paused at the new position while a playing one keeps playing. `position` derives from the audio clock when audio is active, otherwise from the playback clock, and is zero when EMPTY or STOPPED. `is_finished` is true only at natural end of media with all buffered output drained; demux EOF with queued frames is not finished. `set_volume` clamps to `[0, 1]`. `set_video_hwnd(NULL)` detaches the render target. Every failing call returns a nonzero `ElyResult` and `ely_get_last_error` explains the most recent failure on that handle until the next failing call replaces it.

## Error model

The `ElyResult` values in the header are the complete v1 set: generic, bad argument, file not found, unsupported format, bad container, bad stream, decode failure, audio device failure, video target failure, seek failure, bad state. Fatal worker failures inside the engine promote the player to ERROR; the shell must never crash because a file is bad, which is why Layer C converts every nonzero code into a Python exception carrying the code, its name, and the engine's message.

## The Phase 1 stub

`src/elysian_video_stub.c` implements the complete contract with simulated media: extension-based classification, fixed fake media info, a ten second duration, and a genuinely running monotonic clock, so play advances position, pause freezes it, seek moves it, and the media naturally ENDs. No demuxing, decoding, or output. Its purpose is that the bindings, the wrapper, and later the shell integrate against exact contract behavior before any codec exists.

## Roadmap and the Phase 3 decision

Phases follow the execution plan: contract (done), stub (done), engine infrastructure, MP4 demux, audio decode and output, video decode and render, validation, then shell integration; all done. The code behind this ABI is native: FFmpeg-backed demux and decode (libavformat/libavcodec/libswresample/libswscale), a software-clocked audio sink pending a device backend, and Win32 StretchDIBits video output. The ABI was frozen first so those internals could mature, and later change backend entirely, without touching the shell-facing contract - which is exactly what happened moving from owned to FFmpeg-backed internals. Only decode-side FFmpeg libraries are linked; nothing in the shipped engine depends on an encoder.

## Future extensions

When these arrive, they arrive as appended functions, never as changes to existing structs or signatures. Subtitles: new query/select/render functions. Multiple audio or video tracks: new enumeration and selection functions. Hardware decode and render choices: invisible implementation detail behind the current ABI, never surfaced through it. Network streams: out of scope for v1 entirely, and a future streaming story would be a new load-style entry point rather than new meanings for `ely_load`.

## v1 exclusions

No DRM or encrypted files, no fragmented MP4, no subtitle tracks, no edit-list edge cases, no tolerance guarantees for corrupt files beyond failing with a clean error. Documented here rather than pretended.
