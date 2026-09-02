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

/* Centralized packet lifetime: dispose frees the payload and zeroes the
 * packet; clear disposes everything queued; queue_free clears first, so a
 * torn-down queue can never leak payloads. */
void packet_dispose(Packet* p);
void queue_clear(PacketQueue* q);
