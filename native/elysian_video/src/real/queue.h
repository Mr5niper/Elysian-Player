#pragma once
#include "frame.h"

/* Bounded ring for the stepped pipeline (demux -> decode -> output).
 * Packet.data ownership: see frame.h. */
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
