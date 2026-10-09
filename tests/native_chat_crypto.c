/* t27/chat_crypto.t27 under ASan and UBSan: the published RFC 8439, FIPS 180-4,
 * RFC 4231 and RFC 5869 vectors, the TGE1 envelope round trip, and every open
 * status of specs/memory/graph/crypt.t27 (issue #136). */
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#if defined(__clang__)
#pragma clang diagnostic ignored "-Wparentheses-equality"
#endif
#include "chat_crypto.h"

static size_t from_hex(const char *hex, uint8_t *out) {
    size_t n = strlen(hex) / 2;
    for (size_t i = 0; i < n; ++i) {
        unsigned value = 0;
        assert(sscanf(hex + 2 * i, "%2x", &value) == 1);
        out[i] = (uint8_t)value;
    }
    return n;
}

static void expect(const uint8_t *got, const char *hex) {
    uint8_t want[256];
    size_t n = from_hex(hex, want);
    assert(memcmp(got, want, n) == 0);
}

int main(void) {
    uint8_t key[32], nonce[12], out[256];
    for (int i = 0; i < 32; ++i) key[i] = (uint8_t)i;
    from_hex("000000090000004a00000000", nonce);
    cm_chacha20_block(key, 1, nonce, out);
    expect(out, "10f1e7e4d13b5915500fdd1fa32071c4c7d1f4c733c068030422aa9ac3d46c4e"
                "d2826446079faa0914c2d705d98b02a2b5129cd1de164eb9cbd083e8a2503c4e");

    uint8_t poly_key[32];
    from_hex("85d6be7857556d337f4452fe42d506a80103808afb0db2fd4abff6af4149f51b", poly_key);
    const char *forum = "Cryptographic Forum Research Group";
    cm_poly1305(poly_key, (uint8_t *)forum, strlen(forum), out);
    expect(out, "a8061dc1305136c6c22b8baf0c0127a9");

    from_hex("808182838485868788898a8b8c8d8e8f909192939495969798999a9b9c9d9e9f", key);
    from_hex("070000004041424344454647", nonce);
    uint8_t aad[12], ct[128], tag[16];
    from_hex("50515253c0c1c2c3c4c5c6c7", aad);
    const char *sunscreen = "Ladies and Gentlemen of the class of '99: If I could offer you only one tip for the "
                            "future, sunscreen would be it.";
    size_t n = strlen(sunscreen);
    cm_aead_seal(key, nonce, aad, 12, (uint8_t *)sunscreen, n, ct, tag);
    expect(ct, "d31a8d34648e60db7b86afbc53ef7ec2a4aded51296e08fea9e2b5a736ee62d63dbea45e8ca9671282fafb69da92728b"
               "1a71de0a9e060b2905d6a5b67ecd3b3692ddbd7f2d778b8c9803aee328091b58fab324e4fad675945585808b4831d7bc"
               "3ff4def08e4b7a9de576d26586cec64b6116");
    expect(tag, "1ae10b594f09e26a7e902ecbd0600691");

    cm_sha256((uint8_t *)"abc", 3, out);
    expect(out, "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
    cm_sha256((uint8_t *)"", 0, out);
    expect(out, "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
    cm_hmac_sha256((uint8_t *)"Jefe", 4, (uint8_t *)"what do ya want for nothing?", 28, out);
    expect(out, "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843");
    uint8_t ikm[22], salt[13], info[10];
    memset(ikm, 0x0b, sizeof ikm);
    from_hex("000102030405060708090a0b0c", salt);
    from_hex("f0f1f2f3f4f5f6f7f8f9", info);
    assert(cm_hkdf_sha256(salt, 13, ikm, 22, info, 10, out, 42) == 0);
    expect(out, "3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db02d56ecc4c5bf34007208d5b887185865");
    assert(cm_hkdf_sha256(salt, 13, ikm, 22, info, 10, out, 8161) == -1);

    uint8_t env[256], back[200];
    for (int i = 0; i < 12; ++i) nonce[i] = (uint8_t)i;
    int64_t size = cm_env_seal(key, 1, nonce, 1, 7, 8, 9, 10, (uint8_t *)sunscreen, n, env, sizeof env);
    assert(size == (int64_t)n + 40);
    assert(cm_env_seal(key, 0, nonce, 1, 7, 8, 9, 10, (uint8_t *)sunscreen, n, env, sizeof env) == -1);
    assert(cm_env_seal(key, 1, nonce, 1, 7, 8, 9, 10, (uint8_t *)sunscreen, n, env, n + 39) == -2);
    assert(cm_env_key_version(env, (size_t)size) == 1);
    assert(cm_env_open(key, 1, 1, true, 1, 7, 8, 9, 10, env, (size_t)size, back, sizeof back) == (int64_t)n);
    assert(memcmp(back, sunscreen, n) == 0);
    assert(cm_env_open(key, 2, 1, true, 1, 7, 8, 9, 10, env, (size_t)size, back, sizeof back) == (int64_t)n);
    assert(cm_env_open(key, 1, 1, true, 1, 7, 99, 9, 10, env, (size_t)size, back, sizeof back) == -5);
    assert(cm_env_open(key, 1, 1, true, 1, 7, 8, 5, 10, env, (size_t)size, back, sizeof back) == -5);
    assert(cm_env_open(key, 1, 1, true, 2, 7, 8, 9, 10, env, (size_t)size, back, sizeof back) == -5);
    assert(cm_env_open(key, 1, 1, false, 1, 7, 8, 9, 10, env, (size_t)size, back, sizeof back) == -4);
    assert(cm_env_open(key, 3, 1, true, 1, 7, 8, 9, 10, env, (size_t)size, back, sizeof back) == -2);
    assert(cm_env_open(key, 0, 1, true, 1, 7, 8, 9, 10, env, (size_t)size, back, sizeof back) == -2);
    assert(cm_env_open(key, 1, 2, true, 1, 7, 8, 9, 10, env, (size_t)size, back, sizeof back) == -3);
    assert(cm_env_open(key, 1, 1, true, 1, 7, 8, 9, 10, env, 39, back, sizeof back) == -1);
    assert(cm_env_open(key, 1, 1, true, 1, 7, 8, 9, 10, env, (size_t)size, back, n - 1) == -6);
    env[24] ^= 1;
    assert(cm_env_open(key, 1, 1, true, 1, 7, 8, 9, 10, env, (size_t)size, back, sizeof back) == -5);
    env[24] ^= 1;
    env[5] = 2;
    assert(cm_env_open(key, 1, 1, true, 1, 7, 8, 9, 10, env, (size_t)size, back, sizeof back) == -1);
    printf("PASS t27/chat_crypto: RFC 8439 block/Poly1305/AEAD, FIPS 180-4, RFC 4231, RFC 5869, "
           "TGE1 round trip and open status order\n");
    return 0;
}
