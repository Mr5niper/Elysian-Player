@echo off
setlocal
:: Builds elysian_video_real.dll from the real-engine sources with MSVC.
:: Run from a "x64 Native Tools Command Prompt for VS".

cl /nologo /EHsc /std:c++17 /O2 /W4 /LD /DELY_VIDEO_EXPORTS ^
  /Iinclude ^
  src\real\abi_exports.cpp ^
  src\real\player.cpp ^
  src\real\clock.cpp ^
  src\real\queue.cpp ^
  src\real\mp4_demux.cpp ^
  src\real\aac_decode.cpp ^
  src\real\h264_decode.cpp ^
  src\real\audio_out.cpp ^
  src\real\video_out.cpp ^
  /Fe:elysian_video_real.dll ^
  /link ole32.lib user32.lib gdi32.lib

if errorlevel 1 (
  echo Build failed.
  exit /b 1
)
echo Built elysian_video_real.dll
endlocal
