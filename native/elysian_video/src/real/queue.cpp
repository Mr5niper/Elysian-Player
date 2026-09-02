#include "queue.h"
#include <stdlib.h>
#include <string.h>

int queue_init(PacketQueue* q, int cap) {
    if (cap <= 0) return 0;
    q->items = (Packet*)calloc((size_t)cap, sizeof(Packet));
    if (!q->items) return 0;
    q->cap = cap;
    q->head = q->tail = q->count = 0;
    return 1;
}

void queue_free(PacketQueue* q) {
    free(q->items);
    memset(q, 0, sizeof(*q));
}

int queue_push(PacketQueue* q, Packet p) {
    if (q->count >= q->cap) return 0;
    q->items[q->tail] = p;
    q->tail = (q->tail + 1) % q->cap;
    q->count++;
    return 1;
}

int queue_pop(PacketQueue* q, Packet* out) {
    if (q->count <= 0) return 0;
    *out = q->items[q->head];
    q->head = (q->head + 1) % q->cap;
    q->count--;
    return 1;
}
