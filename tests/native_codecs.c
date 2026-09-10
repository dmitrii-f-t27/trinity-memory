/* Independent native acceptance harness. Generate codecs.h/container.h from
 * t27 sources into an include directory; compile this file with that -I path.
 * No native production implementation is maintained in this test file. */
#include <assert.h>
#include <inttypes.h>
#include <limits.h>
#include <stdio.h>
#include <string.h>
#include "codecs.h"
#include "container.h"

static uint32_t random_state = 2709;
static uint32_t next_random(void) {
    random_state = random_state * UINT32_C(1664525) + UINT32_C(1013904223);
    return random_state;
}

/* Independent bit-level writer avoids production's shift-buffer algorithm. */
static void store_bits(uint8_t *out, size_t offset, uint64_t word, unsigned bits) {
    for (unsigned i = 0; i < bits; ++i)
        out[(offset + i) / 8] |= (uint8_t)(((word >> i) & 1) << ((offset + i) % 8));
}

static void exhaustive_dense_and_baseline(void) {
    int32_t values[5], restored[5];
    uint8_t bytes[2];
    for (unsigned code = 0; code < 243; ++code) {
        unsigned n = code;
        for (unsigned i = 0; i < 5; ++i) { values[i] = (int32_t)(n % 3) - 1; n /= 3; }
        assert(tm_encode(1, values, 5, bytes, 2) == 1);
        assert(bytes[0] == code);
        assert(tm_decode(1, bytes, 1, 5, restored, 5) == 0);
        assert(memcmp(values, restored, sizeof values) == 0);
    }
    for (unsigned code = 243; code < 256; ++code) {
        bytes[0] = (uint8_t)code;
        assert(tm_decode(1, bytes, 1, 5, restored, 5) == TM_ERR_CODE);
    }
    for (unsigned code = 0; code < 256; ++code) {
        bytes[0] = (uint8_t)code;
        int valid = 1;
        for (unsigned i = 0; i < 4; ++i) {
            unsigned lane = (code >> (2 * i)) & 3;
            if (lane == 3) valid = 0;
            values[i] = lane == 2 ? -1 : (int32_t)lane;
        }
        int status = tm_decode(0, bytes, 1, 4, restored, 4);
        if (!valid) { assert(status == TM_ERR_CODE); continue; }
        assert(status == 0 && memcmp(values, restored, 4 * sizeof(int32_t)) == 0);
        assert(tm_encode(0, values, 4, bytes, 2) == 1 && bytes[0] == code);
    }
}

static void exhaustive_sparse(int codec, int width, int maximum) {
    /* Construct the declared ordering independently as a lookup table. */
    int32_t expected[129][8] = {{0}};
    int states = 1;
    for (int position = 0; position < width; ++position) {
        expected[states++][position] = -1;
        expected[states++][position] = 1;
    }
    if (maximum == 2) {
        for (int a = 0; a < width; ++a) for (int b = a + 1; b < width; ++b) {
            for (int sign = 0; sign < 4; ++sign) {
                expected[states][a] = sign & 1 ? 1 : -1;
                expected[states][b] = sign & 2 ? 1 : -1;
                ++states;
            }
        }
    }
    assert(states == (codec == 4 ? 9 : 129));
    uint8_t bytes[1]; int32_t decoded[8];
    for (int code = 0; code < states; ++code) {
        assert(tm_encode(codec, expected[code], (size_t)width, bytes, 1) == 1);
        assert(bytes[0] == code);
        assert(tm_decode(codec, bytes, 1, (size_t)width, decoded, (size_t)width) == 0);
        assert(memcmp(decoded, expected[code], (size_t)width * sizeof(int32_t)) == 0);
    }
    int combinations = 1;
    for (int i = 0; i < width; ++i) combinations *= 3;
    for (int i = 0; i < combinations; ++i) {
        int v = i, nonzero = 0; int32_t values[8];
        for (int j = 0; j < width; ++j) { values[j] = v % 3 - 1; v /= 3; nonzero += values[j] != 0; }
        int64_t status = tm_encode(codec, values, (size_t)width, bytes, 1);
        if (nonzero > maximum) assert(status == TM_ERR_SPARSITY);
        else assert(status == 1);
    }
    for (int code = states; code < (codec == 4 ? 16 : 256); ++code) {
        bytes[0] = (uint8_t)code;
        assert(tm_decode(codec, bytes, 1, (size_t)width, decoded, (size_t)width) == TM_ERR_CODE);
    }
}

static void lengths_and_independent_bit_layout(void) {
    static const size_t group[] = {4, 5, 17, 22, 4, 8};
    static const unsigned bits[] = {8, 8, 27, 35, 4, 8};
    int32_t values[2048], decoded[2048];
    uint8_t encoded[1024], expected[1024], file[1048];
    for (int codec = 0; codec < 6; ++codec) {
        for (size_t count = 0; count <= 2048; ++count) {
            for (size_t i = 0; i < count; ++i) {
                values[i] = (int32_t)(next_random() % 3) - 1;
                if (codec >= 4 && i % group[codec] != 0) values[i] = 0;
            }
            size_t groups = (count + group[codec] - 1) / group[codec];
            size_t size = (groups * bits[codec] + 7) / 8;
            assert(tm_payload_size(codec, count) == (int64_t)size);
            memset(encoded, 0xa5, sizeof encoded);
            assert(tm_encode(codec, values, count, encoded, sizeof encoded) == (int64_t)size);
            assert(tm_decode(codec, encoded, size, count, decoded, 2048) == 0);
            assert(memcmp(values, decoded, count * sizeof(int32_t)) == 0);
            if (codec < 4) {
                memset(expected, 0, sizeof expected);
                for (size_t g = 0; g < groups; ++g) {
                    uint64_t word = 0;
                    /* Reverse Horner order is distinct from the encoder's factor accumulator. */
                    for (size_t lane = group[codec]; lane > 0; --lane) {
                        size_t index = g * group[codec] + lane - 1;
                        int32_t trit = index < count ? values[index] : 0;
                        if (codec == 0) word = word * 4 + (trit == -1 ? 2 : (uint64_t)trit);
                        else word = word * 3 + (uint64_t)(trit + 1);
                    }
                    store_bits(expected, g * bits[codec], word, bits[codec]);
                }
                assert(memcmp(encoded, expected, size) == 0);
            }
            assert(tm_pack(codec, values, count, file, sizeof file) == (int64_t)(24 + size));
            assert(tm_unpack(file, 24 + size, decoded, 2048) == (int64_t)count);
            assert(memcmp(decoded, values, count * sizeof(int32_t)) == 0);
        }
        /* Arithmetic must remain defined for the full uint64 count domain. */
        uint64_t groups = UINT64_MAX / group[codec] + (UINT64_MAX % group[codec] != 0);
        uint64_t expected_size = (groups / 8) * bits[codec] + ((groups % 8) * bits[codec] + 7) / 8;
        assert(tm_payload_size(codec, UINT64_MAX) == (int64_t)expected_size);
    }
}

/* Independent reference CRC, using a table rather than production's bit loop. */
static uint32_t reference_crc(uint8_t *data, size_t n, uint32_t state) {
    uint32_t table[256];
    for (unsigned i = 0; i < 256; ++i) {
        uint32_t c = i;
        for (unsigned j = 0; j < 8; ++j) c = (c >> 1) ^ ((c & 1) ? UINT32_C(0xedb88320) : 0);
        table[i] = c;
    }
    for (size_t i = 0; i < n; ++i) state = table[(state ^ data[i]) & 255] ^ (state >> 8);
    return state;
}

static void reseal(uint8_t *data, size_t size) {
    uint32_t c = reference_crc(data, 20, UINT32_MAX);
    c = reference_crc(data + 24, size - 24, c) ^ UINT32_MAX;
    for (unsigned i = 0; i < 4; ++i) data[20 + i] = (uint8_t)(c >> (8 * i));
}

static void strict_rejection_and_atomic_output(void) {
    int32_t values[22] = {0}, output[22], untouched[22];
    uint8_t payload[16], data[64], damaged[64], previous[64];
    memset(untouched, 0x5a, sizeof untouched);
    for (int codec = 0; codec < 6; ++codec) {
        size_t group = tm_group_size(codec);
        size_t size = (size_t)tm_encode(codec, values, 1, payload, sizeof payload);
        memcpy(output, untouched, sizeof output);
        assert(tm_decode(codec, payload, size, 1, output, 0) == TM_ERR_CAPACITY);
        assert(memcmp(output, untouched, sizeof output) == 0);
        assert(tm_decode(codec, payload, size - 1, 1, output, 22) == TM_ERR_LENGTH);
        assert(tm_decode(codec, payload, size + 1, 1, output, 22) == TM_ERR_LENGTH);
        values[group - 1] = 1;
        assert(tm_encode(codec, values, group, payload, sizeof payload) == (int64_t)size);
        assert(tm_decode(codec, payload, size, 1, output, 22) == TM_ERR_PADDING);
        assert(memcmp(output, untouched, sizeof output) == 0);
        values[group - 1] = 0;
        if (tm_group_bits(codec) % 8 != 0) {
            assert(tm_encode(codec, values, 1, payload, sizeof payload) == (int64_t)size);
            payload[size - 1] |= 128;
            assert(tm_decode(codec, payload, size, 1, output, 22) == TM_ERR_PADDING);
        }
        size_t file_size = (size_t)tm_pack(codec, values, 22, data, sizeof data);
        assert(file_size >= 24 && file_size <= sizeof data);
        for (size_t pos = 0; pos < file_size; ++pos) {
            for (unsigned bit = 0; bit < 8; ++bit) {
                memcpy(damaged, data, file_size); damaged[pos] ^= (uint8_t)(1U << bit);
                memcpy(output, untouched, sizeof output);
                assert(tm_unpack(damaged, file_size, output, 22) < 0);
                assert(memcmp(output, untouched, sizeof output) == 0);
            }
        }
        for (size_t cut = 0; cut < file_size; ++cut) assert(tm_unpack(data, cut, output, 22) < 0);
        assert(tm_unpack(data, file_size + 1, output, 22) < 0);
        memcpy(damaged, data, file_size); damaged[6] = 1; reseal(damaged, file_size);
        assert(tm_unpack(damaged, file_size, output, 22) == TM_ERR_HEADER);
        /* Invalid input must leave both header and payload untouched. */
        memset(data, 0x7e, sizeof data); memcpy(previous, data, sizeof data);
        values[21] = 2;
        assert(tm_pack(codec, values, 22, data, sizeof data) == TM_ERR_TRIT);
        assert(memcmp(data, previous, sizeof data) == 0); values[21] = 0;
        assert(tm_pack(codec, values, 22, data, 24) == TM_ERR_CAPACITY);
        assert(memcmp(data, previous, sizeof data) == 0);
    }
    assert(tm_payload_size(-1, 0) == TM_ERR_CODEC && tm_payload_size(6, 0) == TM_ERR_CODEC);
    assert(tm_encode(99, values, 1, data, sizeof data) == TM_ERR_CODEC);
    assert(tm_decode(99, data, 0, 0, output, 22) == TM_ERR_CODEC);
    assert(tm_pack(0, values, SIZE_MAX, data, sizeof data) == TM_ERR_LIMIT);
    /* Valid CRC must not hide illegal dense5 code or nonzero padded trits. */
    assert(tm_pack(1, values, 5, data, sizeof data) == 25); data[24] = 255; reseal(data, 25);
    assert(tm_unpack(data, 25, output, 22) == TM_ERR_CODE);
    assert(tm_pack(1, values, 1, data, sizeof data) == 25); data[24] = 202; reseal(data, 25);
    assert(tm_unpack(data, 25, output, 22) == TM_ERR_PADDING);
}

static void independent_tmem_fixture(void) {
    uint8_t fixture[25] = {84,77,69,77,1,0,0,0,1,0,0,0,0,0,0,0,1,0,0,0,0,0,0,0,2};
    uint8_t result[25]; int32_t negative = -1, decoded = 0;
    reseal(fixture, sizeof fixture);
    assert(tm_unpack(fixture, sizeof fixture, &decoded, 1) == 1 && decoded == -1);
    assert(tm_pack(0, &negative, 1, result, sizeof result) == 25);
    assert(memcmp(result, fixture, sizeof fixture) == 0);
    assert(tm_crc32((uint8_t *)"123456789", 9) == UINT32_C(0xcbf43926));
    assert(tm_crc32(NULL, 0) == 0);
    for (int codec = 0; codec < 6; ++codec) {
        assert(tm_encode(codec, NULL, 0, NULL, 0) == 0);
        assert(tm_decode(codec, NULL, 0, 0, NULL, 0) == 0);
        assert(tm_pack(codec, NULL, 0, result, 24) == 24);
        assert(tm_unpack(result, 24, NULL, 0) == 0);
    }
}

int main(void) {
    exhaustive_dense_and_baseline();
    exhaustive_sparse(4, 4, 1);
    exhaustive_sparse(5, 8, 2);
    lengths_and_independent_bit_layout();
    strict_rejection_and_atomic_output();
    independent_tmem_fixture();
    puts("PASS native codecs/TMEM: exhaustive dense5/baseline/sparse states; all codecs lengths0..2048; independent bit layout/CRC/framing; corruption/padding/bounds/atomic errors");
    return 0;
}
