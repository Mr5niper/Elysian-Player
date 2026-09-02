# Linux/macOS test build of the stub engine. The DLL for the real app is
# produced on Windows with BUILD_DLL.bat; this .so exists so the ABI and the
# Python bindings can be exercised anywhere.
CC ?= gcc
CFLAGS ?= -O2 -Wall -Wextra -fPIC -fvisibility=hidden

libelysian_video.so: src/elysian_video_stub.c include/elysian_video.h
	$(CC) $(CFLAGS) -shared -o $@ src/elysian_video_stub.c

clean:
	rm -f libelysian_video.so
