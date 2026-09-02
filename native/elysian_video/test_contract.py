"""Executable contract test for the elysian_video ABI.

Runs against any implementation of the frozen header: the Linux test .so,
the Windows stub DLL, and eventually the real engine. Usage:

    python test_contract.py [path-to-library]

With no argument the library is found the same way the app finds it. Every
assertion here is a sentence from CONTRACT.md; if an implementation fails
this file, the implementation is wrong, not the test.
"""
import ctypes
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[2]))

from elysian.playback.bindings import (ABI_VERSION, ElyMediaInfo,
                                       MEDIA_AUDIO, MEDIA_VIDEO, NativeLib,
                                       VideoEngineError, find_library)
from elysian.playback.video_engine import VideoPlaybackEngine


def main() -> int:
    lib_path = sys.argv[1] if len(sys.argv) > 1 else find_library()
    assert lib_path, "no engine library found; build it first"
    print(f"testing {lib_path}")

    lib = NativeLib(lib_path)
    assert lib.lib.ely_abi_version() == ABI_VERSION
    print(f"abi version {ABI_VERSION} confirmed")

    # fixtures: structurally valid MP4s, since a real demuxer must reject
    # fake bytes (the one-byte fixtures were stub-shaped). The unicode name
    # remains the waveform lesson. Audio is .m4a: v1 engines are MP4-family.
    sys.path.insert(0, str(HERE.parent))
    from make_fixture import write_mp4
    tmp = Path(tempfile.mkdtemp())
    vid = tmp / "clip.mp4"
    write_mp4(vid, video=True)
    uni = tmp / "clip - Renée's 動画.mp4"
    write_mp4(uni, video=True)
    aud = tmp / "song.m4a"
    write_mp4(aud, video=False)

    p = lib.lib.ely_create_player()
    assert p, "create_player returned NULL"

    # state machine: EMPTY rejects transport
    assert lib.lib.ely_play(p) != 0, "play on EMPTY must fail"
    assert lib.lib.ely_get_state(p) == 0  # EMPTY
    err = lib.lib.ely_get_last_error(p)
    assert isinstance(err, str) and err, "last_error must explain the failure"
    print("EMPTY state rejects play, error message present:", repr(err))

    # load + media info via the struct_size handshake
    assert lib.lib.ely_load(p, str(vid)) == 0
    info = ElyMediaInfo()
    info.struct_size = ctypes.sizeof(ElyMediaInfo)
    assert lib.lib.ely_get_media_info(p, ctypes.byref(info)) == 0
    assert info.kind == MEDIA_VIDEO and info.has_video and info.width > 0
    assert info.duration > 0
    print(f"load + media info: {info.width}x{info.height}, "
          f"{info.duration:.1f}s, kind=video")

    # unsupported and missing files fail with the right classes
    bad = tmp / "notes.txt"
    bad.write_text("x")
    assert lib.lib.ely_load(p, str(bad)) == 4, "txt must be UNSUPPORTED"
    assert lib.lib.ely_load(p, str(tmp / "ghost.mp4")) == 3, \
        "missing file must be NOT_FOUND"
    print("unsupported and missing files fail with correct codes")

    # unicode path loads (wchar_t contract)
    assert lib.lib.ely_load(p, str(uni)) == 0, "unicode path must load"
    print("unicode filename loads cleanly")

    # clock semantics: play advances, pause freezes, seek moves, stop zeroes
    assert lib.lib.ely_load(p, str(vid)) == 0
    assert lib.lib.ely_play(p) == 0
    time.sleep(0.25)
    pos = lib.lib.ely_get_position(p)
    assert 0.15 < pos < 0.6, f"position should advance while playing: {pos}"
    assert lib.lib.ely_pause(p) == 0
    frozen = lib.lib.ely_get_position(p)
    time.sleep(0.2)
    assert abs(lib.lib.ely_get_position(p) - frozen) < 0.01, \
        "pause must freeze the clock"
    assert lib.lib.ely_seek(p, 5.0) == 0
    assert abs(lib.lib.ely_get_position(p) - 5.0) < 0.01
    assert lib.lib.ely_is_paused(p), "seek while paused stays paused"
    assert lib.lib.ely_seek(p, 999.0) == 0
    assert lib.lib.ely_get_position(p) <= lib.lib.ely_get_duration(p), \
        "seek must clamp to duration"
    assert lib.lib.ely_stop(p) == 0
    assert lib.lib.ely_get_position(p) == 0.0
    assert lib.lib.ely_get_state(p) == 4  # STOPPED, file still loaded
    print("clock: advance, pause-freeze, seek, clamp, stop all correct")

    # end of media: finished only at natural EOF
    assert lib.lib.ely_play(p) == 0             # restart from 0
    assert lib.lib.ely_seek(p, lib.lib.ely_get_duration(p) - 0.15) == 0
    assert not lib.lib.ely_is_finished(p)
    time.sleep(0.3)
    assert lib.lib.ely_is_finished(p), "must finish at natural EOF"
    assert lib.lib.ely_get_state(p) == 5        # ENDED
    assert lib.lib.ely_play(p) == 0             # ENDED -> restart
    assert lib.lib.ely_get_position(p) < 0.2
    print("EOF: finished at natural end, restart from ENDED works")

    # volume clamps; hwnd binding accepts and detaches
    assert lib.lib.ely_set_volume(p, ctypes.c_float(2.0)) == 0
    assert abs(lib.lib.ely_get_volume(p) - 1.0) < 0.001
    assert lib.lib.ely_set_video_hwnd(p, 0xDEAD) == 0
    assert lib.lib.ely_resize_video(p, 800, 450) == 0
    assert lib.lib.ely_set_video_hwnd(p, None) == 0
    assert lib.lib.ely_resize_video(p, -1, 5) != 0
    print("volume clamp, hwnd bind/detach, resize validation correct")

    lib.lib.ely_destroy_player(p)

    # bad-state edges: every transition rejection the contract names
    q = lib.lib.ely_create_player()
    BAD_STATE = 11
    assert lib.lib.ely_stop(q) == BAD_STATE, "stop on EMPTY"
    assert lib.lib.ely_seek(q, 1.0) == BAD_STATE, "seek on EMPTY"
    info2 = ElyMediaInfo()
    info2.struct_size = ctypes.sizeof(ElyMediaInfo)
    assert lib.lib.ely_get_media_info(q, ctypes.byref(info2)) == BAD_STATE, \
        "media_info on EMPTY"
    assert lib.lib.ely_load(q, str(vid)) == 0
    assert lib.lib.ely_pause(q) == BAD_STATE, "pause on LOADED"
    assert lib.lib.ely_resume(q) == BAD_STATE, "resume on LOADED"
    print("bad-state edges: EMPTY and LOADED reject exactly as specified")

    # play while PLAYING is an idempotent success that does not reset time
    assert lib.lib.ely_play(q) == 0
    time.sleep(0.2)
    before = lib.lib.ely_get_position(q)
    assert lib.lib.ely_play(q) == 0, "play while PLAYING must succeed"
    assert lib.lib.ely_get_position(q) >= before - 0.01, \
        "idempotent play must not rewind"
    print("play while PLAYING: idempotent, position preserved")

    # ENDED -> seek backward -> PAUSED, never silently playing
    assert lib.lib.ely_seek(q, lib.lib.ely_get_duration(q)) == 0
    time.sleep(0.05)
    assert lib.lib.ely_is_finished(q)
    assert lib.lib.ely_seek(q, 3.0) == 0
    assert lib.lib.ely_get_state(q) == 3, "seek back from ENDED must pause"
    assert abs(lib.lib.ely_get_position(q) - 3.0) < 0.01
    print("ENDED then seek backward lands PAUSED at the target")

    # unload returns to EMPTY from LOADED, PAUSED and ENDED alike
    assert lib.lib.ely_unload(q) == 0 and lib.lib.ely_get_state(q) == 0
    assert lib.lib.ely_load(q, str(vid)) == 0
    assert lib.lib.ely_unload(q) == 0 and lib.lib.ely_get_state(q) == 0
    assert lib.lib.ely_load(q, str(vid)) == 0
    assert lib.lib.ely_play(q) == 0
    assert lib.lib.ely_seek(q, lib.lib.ely_get_duration(q)) == 0
    time.sleep(0.05)
    assert lib.lib.ely_get_state(q) == 5
    assert lib.lib.ely_unload(q) == 0 and lib.lib.ely_get_state(q) == 0
    print("unload reaches EMPTY from PAUSED, LOADED and ENDED")

    # error lifetime: success never clears the last failure message
    assert lib.lib.ely_pause(q) == BAD_STATE            # plant a failure
    planted = lib.lib.ely_get_last_error(q)
    assert planted
    assert lib.lib.ely_load(q, str(vid)) == 0           # success after it
    assert lib.lib.ely_get_last_error(q) == planted, \
        "success must not clear last_error"
    print("last_error persists across success, as the contract locks")

    # NULL player: last_error yields the empty string, never a crash
    assert lib.lib.ely_get_last_error(None) == "", \
        "last_error(NULL) must be the empty string"
    print("last_error(NULL) is the empty string")

    # STOPPED keeps the file loaded, so duration stays available
    assert lib.lib.ely_play(q) == 0
    assert lib.lib.ely_stop(q) == 0
    assert lib.lib.ely_get_state(q) == 4                # STOPPED
    assert lib.lib.ely_get_duration(q) > 0, \
        "duration must remain available in STOPPED"
    assert lib.lib.ely_get_position(q) == 0.0
    print("duration survives STOPPED; position is zero there")

    # audio classification through the raw ABI, not just the wrapper
    assert lib.lib.ely_load(q, str(aud)) == 0
    ainfo = ElyMediaInfo()
    ainfo.struct_size = ctypes.sizeof(ElyMediaInfo)
    assert lib.lib.ely_get_media_info(q, ctypes.byref(ainfo)) == 0
    assert ainfo.kind == MEDIA_AUDIO and ainfo.has_audio \
        and not ainfo.has_video and ainfo.width == 0
    print("raw ABI classifies audio files correctly")
    lib.lib.ely_destroy_player(q)

    # Layer C: the shell-facing wrapper over the same library
    eng = VideoPlaybackEngine(lib_path)
    assert eng.available, eng.error
    eng.play(str(vid))
    time.sleep(0.15)
    assert eng.playing and eng.active and not eng.paused
    assert eng.is_video() and eng.width == 1920 and eng.has_video
    eng.toggle()
    assert eng.paused
    eng.nudge(2.0)
    assert eng.position > 2.0
    eng.toggle()
    assert eng.playing
    eng.set_volume(0.4)
    assert abs(eng.volume - 0.4) < 0.001
    eng.stop()
    assert not eng.active and eng.position == 0.0
    # stop from a merely loaded (not active) state is legal per contract
    eng.stop()
    assert eng.duration > 0, "media must remain loaded after stop"
    aud_eng = VideoPlaybackEngine(lib_path)
    aud_eng.load(str(aud))
    assert aud_eng.kind == MEDIA_AUDIO and not aud_eng.has_video
    try:
        eng.load(str(bad))
        raise AssertionError("wrapper must raise on unsupported files")
    except VideoEngineError as exc:
        assert exc.name == "UNSUPPORTED"
    print("VideoPlaybackEngine wrapper: transport, geometry, errors correct")

    # graceful degradation: missing library behaves like the audio engine
    ghost = VideoPlaybackEngine("/nonexistent/elysian_video.dll")
    assert not ghost.available and ghost.error
    ghost.play(str(vid)); ghost.stop(); ghost.toggle()   # all safe no-ops
    assert ghost.position == 0.0 and not ghost.playing
    print("missing library degrades gracefully, all calls are no-ops")

    eng.close(); aud_eng.close(); ghost.close()
    print("ALL CONTRACT TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
