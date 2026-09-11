/* Differential harness: specs/memory/stream_compute.t27 against the executable
 * dot pipeline source t27/rtl/dot_stream.t27, generated to C as rtl_dot_stream.h
 * (the same source the RTL is generated from). Decoding, masks, activations and
 * group validity are compared exhaustively or over seeded random beats; the
 * sequential on_clock is driven with the spec's own combinational rules and
 * compared with a spec-side state machine. No production algorithm lives here. */
#include <assert.h>
#include <inttypes.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#if defined(__clang__)
#pragma clang diagnostic ignored "-Wparentheses-equality"
#endif
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wunused-parameter"
#pragma GCC diagnostic ignored "-Wunused-function"
#include "specs/types.h"
#include "specs/stream_compute.h"
#include "rtl_dot_stream.h"
#include "rtl_stream_join.h"
#pragma GCC diagnostic pop

static uint32_t random_state = 2709;
static uint32_t next_random(void) {
    random_state = random_state * UINT32_C(1664525) + UINT32_C(1013904223);
    return random_state;
}

static void decoding_masks_and_activations(void) {
    for (uint32_t code = 0; code < 1024; ++code) {
        assert(decode((uint16_t)code, true) == tms_dot_decode(code, true));
        assert(decode((uint16_t)code, false) == tms_dot_decode(code, false));
    }
    for (uint32_t mask = 0; mask < 32; ++mask) {
        assert(valid_mask((uint8_t)mask, true) == tms_dot_mask_valid(mask, true));
        assert(valid_mask((uint8_t)mask, false) == tms_dot_mask_valid(mask, false));
    }
    for (uint32_t byte = 0; byte < 256; ++byte) {
        for (uint32_t lane = 0; lane < 5; ++lane) {
            uint64_t acts = (uint64_t)byte << (8 * lane);
            assert(activation(acts, 8 * lane) == tms_dot_activation(acts, lane));
        }
    }
    assert(tms_dot_activation(tms_dot_pack_activations(-128, 127, 0, 1, -1), 4) == -1);
}

static void random_beats(void) {
    for (unsigned trial = 0; trial < 20000; ++trial) {
        uint32_t code = next_random() & 1023;
        bool dense = (next_random() & 1) != 0;
        uint64_t acts = ((uint64_t)next_random() << 32 | next_random()) & ((UINT64_C(1) << 40) - 1);
        uint32_t mask = next_random() & 31;
        bool last = (next_random() & 1) != 0;
        if ((next_random() & 3) == 0) mask = tms_dot_mask_valid(mask, last) ? mask : 31;
        uint32_t lanes = tms_dot_decode(code, dense);
        assert(decode((uint16_t)code, dense) == lanes);
        assert(sum_group((uint16_t)lanes, acts, (uint8_t)mask) == tms_dot_group_sum(lanes, acts, mask));
        assert(valid_group((uint16_t)lanes, acts, (uint8_t)mask, last) == tms_dot_group_valid(lanes, acts, mask, last));
        int32_t sum = tms_dot_group_sum(lanes, acts, mask);
        assert(sum >= TMS_DOT_GROUP_SUM_MIN && sum <= TMS_DOT_GROUP_SUM_MAX);
    }
}

/* Spec-side state machine, written from the contract, run beside the generated on_clock. */
typedef struct {
    bool stage_valid, stage_error, stage_last, stage_empty, frame_error, frame_active, out_valid, out_error;
    int32_t stage_sum, accumulator, out_result;
} SpecState;

typedef struct { bool reset, in_valid, in_last, out_ready; uint32_t code, mask; uint64_t acts; } Beat;

static void spec_clock(SpecState *m, const Beat *b, bool dense, int64_t minimum, int64_t maximum, bool *in_ready_after) {
    bool accumulate_ready = tms_dot_accumulate_ready(m->out_valid, b->out_ready);
    bool in_ready = tms_dot_in_ready(b->reset, m->stage_valid, m->out_valid, b->out_ready);
    uint32_t decoded = tms_dot_decode(b->code, dense);
    int32_t group_sum = tms_dot_group_sum(decoded, b->acts, b->mask);
    bool group_ok = tms_dot_group_valid(decoded, b->acts, b->mask, b->in_last);
    int64_t next_total = (int64_t)m->accumulator + m->stage_sum;
    bool next_error = tms_dot_next_error(m->frame_error, m->stage_error, next_total, minimum, maximum, m->stage_empty, m->frame_active);
    if (b->reset) {
        memset(m, 0, sizeof *m);
    } else {
        if (m->out_valid && b->out_ready) m->out_valid = false;
        if (m->stage_valid && accumulate_ready) {
            m->stage_valid = false;
            if (m->stage_last) {
                m->out_valid = true; m->out_error = next_error; m->out_result = next_error ? 0 : (int32_t)next_total;
                m->accumulator = 0; m->frame_error = false; m->frame_active = false;
            } else {
                m->accumulator = next_error ? 0 : (int32_t)next_total; m->frame_error = next_error; m->frame_active = true;
            }
        }
        if (b->in_valid && in_ready) {
            m->stage_valid = true; m->stage_sum = group_sum; m->stage_error = !group_ok;
            m->stage_last = b->in_last; m->stage_empty = b->mask == 0;
        }
    }
    *in_ready_after = tms_dot_in_ready(b->reset, m->stage_valid, m->out_valid, b->out_ready);
}

/* Drive the generated on_clock: its combinational wires are module-level
 * assignments in the source, supplied here from the spec's rules. */
static void impl_clock(const Beat *b, bool dense, int64_t minimum, int64_t maximum) {
    accumulate_ready = tms_dot_accumulate_ready(out_valid, b->out_ready);
    in_ready = tms_dot_in_ready(b->reset, stage_valid, out_valid, b->out_ready);
    decoded = (uint16_t)tms_dot_decode(b->code, dense);
    group_sum = (int16_t)tms_dot_group_sum(decoded, b->acts, b->mask);
    group_ok = tms_dot_group_valid(decoded, b->acts, b->mask, b->in_last);
    next_total = (int64_t)accumulator + stage_sum;
    next_error = tms_dot_next_error(frame_error, stage_error, next_total, minimum, maximum, stage_empty, frame_active);
    on_clock(b->reset, b->in_valid, (uint16_t)b->code, b->acts, (uint8_t)b->mask, b->in_last, b->out_ready, dense, minimum, maximum);
    in_ready = tms_dot_in_ready(b->reset, stage_valid, out_valid, b->out_ready);
}

static void sequential_equivalence(void) {
    for (unsigned run = 0; run < 64; ++run) {
        uint32_t width = 12 + (next_random() % 21);
        bool dense = (run & 1) != 0;
        int64_t minimum = tms_dot_acc_min(width), maximum = tms_dot_acc_max(width);
        SpecState spec; memset(&spec, 0, sizeof spec);
        Beat reset_beat = {true, false, false, false, 0, 0, 0};
        bool spec_ready;
        spec_clock(&spec, &reset_beat, dense, minimum, maximum, &spec_ready);
        impl_clock(&reset_beat, dense, minimum, maximum);
        for (unsigned cycle = 0; cycle < 400; ++cycle) {
            Beat b;
            b.reset = (next_random() % 41) == 0;
            b.in_valid = (next_random() % 4) != 0;
            uint32_t trits = next_random() % 243;
            b.code = dense ? trits : (uint32_t)tms_dot_decode(trits, true) & 1023;
            if ((next_random() % 23) == 0) b.code = next_random() & 1023;
            b.acts = ((uint64_t)next_random() << 32 | next_random()) & ((UINT64_C(1) << 40) - 1);
            if ((next_random() % 3) == 0) b.acts = tms_dot_pack_activations(127, 127, 127, 127, 127);
            b.in_last = (next_random() % 3) == 0;
            b.mask = 31;
            if (b.in_last) { static const uint32_t tails[] = {0, 1, 3, 7, 15, 31}; b.mask = tails[next_random() % 6]; }
            if ((next_random() % 29) == 0) b.mask = next_random() & 31;
            b.out_ready = (next_random() % 3) != 0;
            spec_clock(&spec, &b, dense, minimum, maximum, &spec_ready);
            impl_clock(&b, dense, minimum, maximum);
            assert(spec.out_valid == out_valid && spec.out_error == out_error && spec.out_result == out_result);
            assert(spec_ready == in_ready);
            assert(spec.stage_valid == stage_valid && spec.accumulator == accumulator && spec.frame_error == frame_error);
        }
    }
}

/* The joined path (issue #15): the combinational join of t27/rtl/stream_join.t27,
 * generated to C as rtl_stream_join.h, against the spec's tms_join_word for every
 * input combination and every tail mask; the hold rule against its definition. */
static void storage_join(void) {
    for (unsigned bits = 0; bits < 16; ++bits) {
        bool word_valid = (bits & 1) != 0, word_last = (bits & 2) != 0;
        bool act_valid = (bits & 4) != 0, dot_ready = (bits & 8) != 0;
        for (uint32_t tail = 0; tail < 256; ++tail) {
            uint32_t expected = tms_join_word(word_valid, word_last, act_valid, dot_ready, tail);
            assert(on_comb(word_valid, word_last, act_valid, dot_ready, (uint8_t)tail) == expected);
            assert(((expected & TMS_JOIN_FIRE_BIT) != 0) == (word_valid && act_valid && dot_ready));
            assert(((expected & TMS_JOIN_WORD_READY_BIT) != 0) == (act_valid && dot_ready));
            assert(((expected & TMS_JOIN_ACT_READY_BIT) != 0) == (word_valid && dot_ready));
            assert(((expected & TMS_JOIN_IN_VALID_BIT) != 0) == (word_valid && act_valid));
            assert((expected >> TMS_JOIN_MASK_SHIFT) == (word_last ? tail : TMS_JOIN_FULL_MASK));
        }
    }
    assert(tms_storage_holds(true, false) && !tms_storage_holds(true, true) && !tms_storage_holds(false, false));
    assert(TMS_STORAGE_HAS_BACKPRESSURE == 1 && TMS_STORAGE_JOIN_NEEDS_BUFFER == 0);
}

int main(void) {
    decoding_masks_and_activations();
    random_beats();
    sequential_equivalence();
    storage_join();
    printf("PASS spec/memory/stream_compute differential harness: exhaustive decode and masks, 256 x 5 activations, "
           "20000 random beats, 64 x 400 sequential cycles against the generated on_clock, join word exhaustive against the generated on_comb\n");
    return 0;
}
