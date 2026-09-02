@echo off
:: Builds elysian_video.dll from the stub source using the MSVC toolchain.
:: Run from a "x64 Native Tools Command Prompt for VS" so cl.exe is on PATH.
:: Output lands next to this script; copy it beside run.py or point the
:: ELYSIAN_VIDEO_DLL environment variable at it.

cl /nologo /O2 /W4 /LD /DELY_VIDEO_EXPORTS ^
   src\elysian_video_stub.c ^
   /Fe:elysian_video.dll ^
   /link /DEF /EXPORT:ely_abi_version

if errorlevel 1 (
  echo Build failed. If cl.exe was not found, install "Build Tools for
  echo Visual Studio" and use its x64 Native Tools prompt, or build with
  echo clang instead:
  echo   clang -O2 -shared -DELY_VIDEO_EXPORTS src\elysian_video_stub.c -o elysian_video.dll
  exit /b 1
)
echo Built elysian_video.dll
