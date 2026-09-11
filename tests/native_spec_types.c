/* Differential harness: the constants and reference functions declared in
 * specs/memory/types.t27 must agree with the executable implementation in
 * t27/codecs.t27 and t27/container.t27. Generate types.h, codecs.h and
 * container.h with the pinned compiler and compile this file with -I paths to
 * both directories. No production algorithm is maintained here. */
#include <assert.h>
#include <inttypes.h>
#include <stdio.h>
#include <string.h>
#include "types.h"
#include "codecs.h"
#include "container.h"

static uint32_t random_state = 2709;
static uint32_t next_random(void) {
    random_state = random_state * UINT32_C(1664525) + UINT32_C(1013904223);
    return random_state;
}

static uint32_t spec_crc32(const uint8_t *data, size_t count) {
    uint32_t state = TMS_CRC32_INIT;
    for (size_t i = 0; i < count; ++i) state = tms_crc32_byte(state, data[i]);
    return state ^ TMS_CRC32_XOR_OUT;
}

static uint64_t read_le(const uint8_t *data, size_t offset, size_t bytes) {
    uint64_t value = 0;
    for (size_t i = 0; i < bytes; ++i) value |= (uint64_t)data[offset + i] << (8 * i);
    return value;
}

static void codec_geometry(void) {
    for (int32_t codec = 0; codec < TMS_CODEC_COUNT; ++codec) {
        assert(tm_group_size(codec) == tms_group_trits(codec));
        assert(tm_group_bits(codec) == tms_group_bits(codec));
        assert(tms_group_trits(codec) > 0 && tms_group_bits(codec) > 0);
    }
    assert(tm_group_size(TMS_CODEC_COUNT) == 0 && tm_group_bits(TMS_CODEC_COUNT) == 0);
    assert(tm_group_size(-1) == 0 && tms_group_trits(-1) == 0);
    assert(tm_payload_size(TMS_CODEC_COUNT, 5) == TMS_ERR_CODEC);
    assert(tm_payload_size(-1, 5) == TMS_ERR_CODEC);
}

static void status_codes(void) {
    assert(TM_ERR_CODEC == TMS_ERR_CODEC && TM_ERR_CAPACITY == TMS_ERR_CAPACITY);
    assert(TM_ERR_TRIT == TMS_ERR_TRIT && TM_ERR_SPARSITY == TMS_ERR_SPARSITY);
    assert(TM_ERR_LENGTH == TMS_ERR_LENGTH && TM_ERR_CODE == TMS_ERR_CODE);
    assert(TM_ERR_PADDING == TMS_ERR_PADDING && TM_ERR_HEADER == TMS_ERR_HEADER);
    assert(TM_ERR_CHECKSUM == TMS_ERR_CHECKSUM && TM_ERR_LIMIT == TMS_ERR_LIMIT);
    /* The implementation reports each documented failure with the spec code. */
    int32_t bad_trit[5] = {2, 0, 0, 0, 0};
    int32_t zeros[5] = {0, 0, 0, 0, 0};
    int32_t two_nonzero[4] = {1, 1, 0, 0};
    int32_t restored[8];
    uint8_t out[8];
    uint8_t code;
    assert(tm_encode(TMS_CODEC_DENSE5, bad_trit, 5, out, sizeof out) == TMS_ERR_TRIT);
    assert(tm_encode(TMS_CODEC_COUNT, zeros, 5, out, sizeof out) == TMS_ERR_CODEC);
    assert(tm_encode(TMS_CODEC_SPARSE41, two_nonzero, 4, out, sizeof out) == TMS_ERR_SPARSITY);
    assert(tm_encode(TMS_CODEC_DENSE5, zeros, 5, out, 0) == TMS_ERR_CAPACITY);
    assert(tm_encode(TMS_CODEC_DENSE5, zeros, 5, out, sizeof out) == 1 && out[0] == TMS_DENSE5_ZERO_GROUP_CODE);
    code = (uint8_t)TMS_DENSE5_RESERVED_FIRST;
    assert(tm_decode(TMS_CODEC_DENSE5, &code, 1, 5, restored, 8) == TMS_ERR_CODE);
    assert(tm_decode(TMS_CODEC_DENSE5, out, 2, 5, restored, 8) == TMS_ERR_LENGTH);
    assert(tm_decode(TMS_CODEC_DENSE5, out, 1, 5, restored, 4) == TMS_ERR_CAPACITY);
    code = (uint8_t)(TMS_DENSE5_ZERO_GROUP_CODE + 81); /* lane 4 holds +1 */
    assert(tm_decode(TMS_CODEC_DENSE5, &code, 1, 4, restored, 8) == TMS_ERR_PADDING);
    assert(tm_decode(TMS_CODEC_DENSE5, &code, 1, 5, restored, 8) == TMS_OK && restored[4] == 1);
}

static void word_validity_and_lanes(void) {
    for (uint64_t word = 0; word < 256; ++word) {
        assert(tm_word_valid(TMS_CODEC_BASELINE2, word) == tms_word_valid(TMS_CODEC_BASELINE2, word));
        assert(tm_word_valid(TMS_CODEC_DENSE5, word) == tms_word_valid(TMS_CODEC_DENSE5, word));
        assert(tm_word_valid(TMS_CODEC_SPARSE82, word) == tms_word_valid(TMS_CODEC_SPARSE82, word));
        if (tm_word_valid(TMS_CODEC_BASELINE2, word))
            for (size_t lane = 0; lane < TMS_BASELINE2_GROUP_TRITS; ++lane)
                assert(tm_word_trit(TMS_CODEC_BASELINE2, word, lane)
                       == tms_lane_trit((uint32_t)((word >> (TMS_LANE_BITS * lane)) & 3)));
    }
    for (uint64_t word = 0; word < 16; ++word)
        assert(tm_word_valid(TMS_CODEC_SPARSE41, word) == tms_word_valid(TMS_CODEC_SPARSE41, word));
    assert(tm_word_valid(TMS_CODEC_DENSE17, TMS_DENSE17_CODE_LIMIT - 1));
    assert(!tm_word_valid(TMS_CODEC_DENSE17, TMS_DENSE17_CODE_LIMIT));
    assert(tm_word_valid(TMS_CODEC_DENSE22, TMS_DENSE22_CODE_LIMIT - 1));
    assert(!tm_word_valid(TMS_CODEC_DENSE22, TMS_DENSE22_CODE_LIMIT));
    /* Every valid word count in the spec equals the implementation's count. */
    for (int32_t codec = 0; codec < TMS_CODEC_COUNT; ++codec) {
        if (codec == TMS_CODEC_DENSE17 || codec == TMS_CODEC_DENSE22) continue;
        uint64_t valid = 0;
        for (uint64_t word = 0; word < ((uint64_t)1 << tm_group_bits(codec)); ++word)
            valid += tm_word_valid(codec, word) ? 1 : 0;
        assert(valid == tms_valid_word_count(codec));
    }
}

static void dense5_reference(void) {
    int32_t values[5];
    uint8_t byte;
    for (unsigned code = 0; code < TMS_DENSE5_CODE_LIMIT; ++code) {
        unsigned n = code;
        for (unsigned i = 0; i < 5; ++i) { values[i] = (int32_t)(n % 3) - 1; n /= 3; }
        assert(tms_dense_code5(values[0], values[1], values[2], values[3], values[4]) == code);
        assert(tm_encode(TMS_CODEC_DENSE5, values, 5, &byte, 1) == 1 && byte == code);
        for (unsigned i = 0; i < 5; ++i) assert(tm_word_trit(TMS_CODEC_DENSE5, code, i) == values[i]);
    }
}

static void payload_sizes(void) {
    for (int32_t codec = 0; codec < TMS_CODEC_COUNT; ++codec) {
        for (uint64_t count = 0; count <= 4096; ++count)
            assert(tm_payload_size(codec, count) == (int64_t)tms_payload_bytes(codec, count));
        assert(tm_payload_size(codec, 65536) == (int64_t)tms_payload_bytes(codec, 65536));
        assert(tm_payload_size(codec, (uint64_t)1 << 20) == (int64_t)tms_payload_bytes(codec, (uint64_t)1 << 20));
        assert(tm_payload_size(codec, (uint64_t)1 << 32) == (int64_t)tms_payload_bytes(codec, (uint64_t)1 << 32));
    }
    assert(tms_payload_bytes(TMS_CODEC_DENSE5, 65536) == 13108);
}

static void crc32_reference(void) {
    uint8_t digits[9] = {'1', '2', '3', '4', '5', '6', '7', '8', '9'};
    uint8_t buffer[512];
    assert(tm_crc32(digits, 9) == TMS_CRC32_CHECK_VALUE);
    assert(spec_crc32(digits, 9) == TMS_CRC32_CHECK_VALUE);
    for (unsigned trial = 0; trial < 64; ++trial) {
        size_t length = next_random() % sizeof buffer;
        for (size_t i = 0; i < length; ++i) buffer[i] = (uint8_t)next_random();
        assert(tm_crc32(buffer, length) == spec_crc32(buffer, length));
    }
}

static void tmem_framing(void) {
    int32_t values[7] = {1, -1, 0, 1, 0, -1, 1};
    int32_t restored[8];
    uint8_t container[64];
    uint8_t payload[8];
    int64_t size = tm_pack(TMS_CODEC_DENSE5, values, 7, container, sizeof container);
    int64_t payload_bytes = (int64_t)tms_payload_bytes(TMS_CODEC_DENSE5, 7);
    assert(size == (int64_t)TMS_TMEM_HEADER_BYTES + payload_bytes);
    assert(container[0] == TMS_TMEM_MAGIC_0 && container[1] == TMS_TMEM_MAGIC_1);
    assert(container[2] == TMS_TMEM_MAGIC_2 && container[3] == TMS_TMEM_MAGIC_3);
    assert(container[TMS_TMEM_VERSION_OFFSET] == TMS_TMEM_VERSION);
    assert(container[TMS_TMEM_CODEC_OFFSET] == TMS_CODEC_DENSE5);
    assert(read_le(container, TMS_TMEM_RESERVED_OFFSET, TMS_TMEM_RESERVED_BYTES) == 0);
    assert(read_le(container, TMS_TMEM_COUNT_OFFSET, TMS_TMEM_COUNT_BYTES) == 7);
    assert(read_le(container, TMS_TMEM_PAYLOAD_LENGTH_OFFSET, TMS_TMEM_PAYLOAD_LENGTH_BYTES) == (uint64_t)payload_bytes);
    /* CRC covers header[0:20] followed by the payload; the payload is the codec output. */
    assert(tm_encode(TMS_CODEC_DENSE5, values, 7, payload, sizeof payload) == payload_bytes);
    assert(memcmp(container + TMS_TMEM_HEADER_BYTES, payload, (size_t)payload_bytes) == 0);
    {
        uint8_t covered[64];
        memcpy(covered, container, TMS_TMEM_CRC_COVERED_HEADER_BYTES);
        memcpy(covered + TMS_TMEM_CRC_COVERED_HEADER_BYTES, payload, (size_t)payload_bytes);
        assert(read_le(container, TMS_TMEM_CRC_OFFSET, TMS_TMEM_CRC_BYTES)
               == spec_crc32(covered, TMS_TMEM_CRC_COVERED_HEADER_BYTES + (size_t)payload_bytes));
    }
    assert(tm_unpack(container, (size_t)size, restored, 8) == 7);
    assert(memcmp(restored, values, sizeof values) == 0);
    container[TMS_TMEM_HEADER_BYTES] ^= 0x01;
    assert(tm_unpack(container, (size_t)size, restored, 8) == TMS_ERR_CHECKSUM);
    container[TMS_TMEM_HEADER_BYTES] ^= 0x01;
    container[0] ^= 0x01;
    assert(tm_unpack(container, (size_t)size, restored, 8) == TMS_ERR_HEADER);
    container[0] ^= 0x01;
    assert(tm_unpack(container, (size_t)size - 1, restored, 8) == TMS_ERR_LENGTH);
    assert(tm_unpack(container, (size_t)TMS_TMEM_HEADER_BYTES - 1, restored, 8) == TMS_ERR_LENGTH);
    container[TMS_TMEM_VERSION_OFFSET] = 2;
    assert(tm_unpack(container, (size_t)size, restored, 8) == TMS_ERR_HEADER);
    container[TMS_TMEM_VERSION_OFFSET] = TMS_TMEM_VERSION;
    assert(tm_unpack(container, (size_t)size, restored, 8) == 7);
    /* Every codec frames one group with its identifier and spec payload size. */
    for (int32_t codec = 0; codec < TMS_CODEC_COUNT; ++codec) {
        int32_t group[22] = {0};
        group[0] = 1;
        size_t count = tms_group_trits(codec);
        size = tm_pack(codec, group, count, container, sizeof container);
        assert(size == (int64_t)TMS_TMEM_HEADER_BYTES + (int64_t)tms_payload_bytes(codec, count));
        assert(container[TMS_TMEM_CODEC_OFFSET] == codec);
        assert(read_le(container, TMS_TMEM_COUNT_OFFSET, TMS_TMEM_COUNT_BYTES) == count);
        assert(tm_unpack(container, (size_t)size, restored, 8) == (int64_t)count || count > 8);
    }
}

int main(void) {
    codec_geometry();
    status_codes();
    word_validity_and_lanes();
    dense5_reference();
    payload_sizes();
    crc32_reference();
    tmem_framing();
    printf("PASS spec/memory/types differential harness: geometry, status codes, "
           "word validity, dense5 reference, payload sizes, CRC32, TMEM framing\n");
    return 0;
}
