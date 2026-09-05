#include "queue.h"
#include <stdlib.h>
#include <utility>

int queue_init(PacketQueue* q, int cap) {
    if (cap <= 0) return 0;
    /* Packet now owns a std::vector, a non-trivial type: calloc-ing raw
     * storage and treating it as a live array of Packet, as the previous
     * version of this function did, would never run Packet's constructor
     * on any of these slots, leaving every vector member in an invalid,
     * uninitialized state rather than the empty-but-valid one a real
     * constructor produces. operator new[] runs the constructor for
     * every element; calloc cannot. */
    q->items = new (std::nothrow) Packet[(size_t)cap];
    if (!q->items) return 0;
    q->cap = cap;
    q->head = q->tail = q->count = 0;
    return 1;
}

void packet_dispose(Packet* p) {
    if (!p) return;
    /* No manual free() needed: data is a std::vector now, and *p = Packet()
     * runs its destructor via the assignment, releasing the buffer, then
     * default-constructs a fresh empty one - the same end state the old
     * free(p->data) + memset(p, 0, sizeof(*p)) produced, without a raw
     * pointer whose lifetime a caller could get wrong. */
    *p = Packet();
}

void queue_clear(PacketQueue* q) {
    if (!q) return;
    Packet pkt;
    while (queue_pop(q, &pkt))
        packet_dispose(&pkt);
}

void queue_free(PacketQueue* q) {
    if (!q) return;
    queue_clear(q);
    delete[] q->items;   /* matches the new[] in queue_init */
    q->items = nullptr;
    q->cap = q->head = q->tail = q->count = 0;
}

int queue_push(PacketQueue* q, Packet p) {
    if (q->count >= q->cap) return 0;
    /* Move, not copy: p is a by-value parameter the caller is done with
     * either way (see player.cpp's call sites, which never touch a
     * Packet again after pushing it), so moving its vector into the
     * queue slot avoids a full payload copy that a plain assignment
     * would otherwise do. */
    q->items[q->tail] = std::move(p);
    q->tail = (q->tail + 1) % q->cap;
    q->count++;
    return 1;
}

int queue_pop(PacketQueue* q, Packet* out) {
    if (q->count <= 0) return 0;
    /* Same reasoning as queue_push: the slot being popped is about to be
     * overwritten by a future push anyway, so there is nothing lost by
     * moving its payload out to the caller instead of copying it. */
    *out = std::move(q->items[q->head]);
    q->head = (q->head + 1) % q->cap;
    q->count--;
    return 1;
}
