This folder holds miniaudio.h, fetched automatically by the root
BUILD_EXE.bat (see its "Ensuring miniaudio.h is present" step) - it is not
committed to the repository and is listed in .gitignore.

What it is:
  miniaudio - a single-header C audio playback/capture library.
  https://github.com/mackron/miniaudio
  License: your choice of public domain or MIT-0 (see the header text
  itself, or third_party/FFMPEG_LICENSE.txt's neighbor once added -
  miniaudio's license is short enough that BUILD_EXE.bat does not fetch
  a separate license file for it; the license text is embedded at the
  bottom of miniaudio.h itself).

Exact version pinned: v0.11.25, commit 9634bedb5b5a2ca38c1ee7108a9358a4e233f14d
  (the tagged release commit, not a floating branch or tag reference -
  see BUILD_EXE.bat for why a commit SHA was chosen over a tag name).

If BUILD_EXE.bat's automatic fetch ever fails (no network, GitHub
unreachable), you can place a working miniaudio.h here yourself:
  1. Download it from:
     https://raw.githubusercontent.com/mackron/miniaudio/9634bedb5b5a2ca38c1ee7108a9358a4e233f14d/miniaudio.h
  2. Save it as third_party/miniaudio.h (this exact folder).
  3. Re-run the build; it will find this file and skip downloading.

Used by: native/elysian_video/src/real/audio_out.cpp (the real audio
device backend), via #include "../../third_party/miniaudio.h".
