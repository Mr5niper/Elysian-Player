#pragma once
#include <stddef.h>

/* Bounded ring for the milestone 2 pipeline (demux -> decode -> output).
 * Single-owner per end; the pipeline threads that will use it are engine
 * internals and never visible through the ABI. Packet data is owned by the
 * producer until popped, then by the consumer. */
struct Packet {
    unsigned char* data;
    size_t size;
    double pts;
    int stream_kind;    /* 1 audio, 2 video, matching ElyMediaKind */
    int keyframe;
};

struct PacketQueue {
    Packet* items;
    int cap;
    int head;
    int tail;
    int count;
};

int queue_init(PacketQueue* q, int cap);
void queue_free(PacketQueue* q);
int queue_push(PacketQueue* q, Packet p);   /* 0 when full */
int queue_pop(PacketQueue* q, Packet* out); /* 0 when empty */
