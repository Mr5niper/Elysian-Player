@echo off
setlocal
:: Builds elysian_video_real.dll from the real-engine sources with MSVC,
:: now linking FFmpeg (avformat/avcodec/avutil/swscale/swresample) for
:: demux and decode. Run from a "x64 Native Tools Command Prompt for VS",
:: or let the root BUILD_EXE.bat call this after it has set up the MSVC
:: environment itself.
::
:: FFmpeg location: FFMPEG_DIR if the caller set it (BUILD_EXE.bat does,
:: pointing at an absolute path), otherwise the repo-root third_party\ffmpeg
:: two levels up from this script, for when this file is run standalone.
:: Either way it must contain include\, lib\*.lib and bin\*.dll from an
:: LGPL SHARED FFmpeg build. See ..\..\NOTICE before changing that build.

if not defined FFMPEG_DIR set "FFMPEG_DIR=..\..\third_party\ffmpeg"

if not exist "%FFMPEG_DIR%\include\libavformat\avformat.h" (
  echo [ERROR] FFmpeg not found at "%FFMPEG_DIR%".
  echo         Run the root BUILD_EXE.bat, which fetches it automatically,
  echo         or place an LGPL SHARED FFmpeg build there yourself first.
  exit /b 1
)

cl /nologo /EHsc /std:c++17 /O2 /W4 /LD /DELY_VIDEO_EXPORTS ^
  /Iinclude ^
  /I"%FFMPEG_DIR%\include" ^
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
  /link /LIBPATH:"%FFMPEG_DIR%\lib" ^
  ole32.lib user32.lib gdi32.lib ^
  avformat.lib avcodec.lib avutil.lib swscale.lib swresample.lib

if errorlevel 1 (
  echo Build failed.
  exit /b 1
)
echo Built elysian_video_real.dll
endlocal
