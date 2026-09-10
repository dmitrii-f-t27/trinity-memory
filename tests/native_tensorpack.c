/* Native framing/descriptor validation, NOT a test of a JSON parser.
 * Headers are generated from the t27 sources into the caller's -I directory. */
#include <assert.h>
#include <float.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "codecs.h"
#include "container.h"
#include "tensorpack.h"

static uint32_t table_crc(const uint8_t *data, size_t n, uint32_t crc) {
    uint32_t table[256];
    for (unsigned i = 0; i < 256; ++i) {
        uint32_t c = i;
        for (unsigned j = 0; j < 8; ++j) c = (c >> 1) ^ ((c & 1) ? UINT32_C(0xedb88320) : 0);
        table[i] = c;
    }
    for (size_t i = 0; i < n; ++i) crc = table[(crc ^ data[i]) & 255] ^ (crc >> 8);
    return crc;
}
static void put32(uint8_t *out, uint32_t x) {
    for (unsigned i = 0; i < 4; ++i) out[i] = (uint8_t)(x >> (8 * i));
}
static void put64(uint8_t *out, uint64_t x) {
    for (unsigned i = 0; i < 8; ++i) out[i] = (uint8_t)(x >> (8 * i));
}
static size_t frame(uint8_t *out, const uint8_t *metadata, size_t m,
                    const uint8_t *payload, size_t n, unsigned tensors) {
    memset(out, 0, 32); memcpy(out, "TTPK", 4); out[4] = 1;
    put32(out + 8, (uint32_t)m); put32(out + 12, tensors); put64(out + 16, n);
    if (m) memcpy(out + 32, metadata, m);
    if (n) memcpy(out + 32 + m, payload, n);
    uint32_t crc = table_crc(out, 24, UINT32_MAX);
    put32(out + 24, table_crc(out + 32, m, crc) ^ UINT32_MAX);
    put32(out + 28, table_crc(out + 32 + m, n, UINT32_MAX) ^ UINT32_MAX);
    return 32 + m + n;
}
static void reseal_outer(uint8_t *out, size_t m, size_t n) {
    uint32_t crc = table_crc(out, 24, UINT32_MAX);
    put32(out + 24, table_crc(out + 32, m, crc) ^ UINT32_MAX);
    put32(out + 28, table_crc(out + 32 + m, n, UINT32_MAX) ^ UINT32_MAX);
}
static void reseal_inner(uint8_t *out, size_t n) {
    uint32_t crc = table_crc(out, 20, UINT32_MAX);
    put32(out + 20, table_crc(out + 24, n - 24, crc) ^ UINT32_MAX);
}
static const char single_json[] =
    "{\"order\":\"C\",\"tensors\":[{\"name\":\"w\",\"shape\":[1],\"axes\":[],"
    "\"codec\":\"dense5\",\"scales\":[1.0],\"scale_axis\":null,\"offset\":0,\"length\":25}]}";

static void utf8_contract(void) {
    uint8_t bytes[8];
    for (unsigned i = 0; i < 128; ++i) {
        bytes[0] = (uint8_t)i;
        assert(tm_tp_utf8(bytes, 1, false));
        assert(tm_tp_utf8(bytes, 1, true) == (i >= 32 && i != 127));
    }
    static const uint8_t valid[][4] = {
        {0xc2,0x80,0,0}, {0xdf,0xbf,0,0}, {0xe0,0xa0,0x80,0},
        {0xed,0x9f,0xbf,0}, {0xee,0x80,0x80,0}, {0xef,0xbf,0xbf,0},
        {0xf0,0x90,0x80,0x80}, {0xf4,0x8f,0xbf,0xbf}
    };
    for (unsigned i = 0; i < sizeof valid / sizeof valid[0]; ++i) {
        size_t n = valid[i][0] < 0xe0 ? 2 : valid[i][0] < 0xf0 ? 3 : 4;
        memcpy(bytes, valid[i], n); assert(tm_tp_utf8(bytes, n, true));
        for (size_t cut = 1; cut < n; ++cut) assert(!tm_tp_utf8(bytes, cut, true));
    }
    static const uint8_t invalid[][4] = {
        {0x80,0x80,0x80,0x80}, {0xc0,0x80,0,0}, {0xc1,0xbf,0,0},
        {0xe0,0x9f,0xbf,0}, {0xed,0xa0,0x80,0}, {0xf0,0x8f,0xbf,0xbf},
        {0xf4,0x90,0x80,0x80}, {0xf5,0x80,0x80,0x80}, {0xff,0,0,0},
        {0xe1,0x7f,0x80,0}, {0xe1,0x80,0xc0,0}
    };
    for (unsigned i = 0; i < sizeof invalid / sizeof invalid[0]; ++i) {
        memcpy(bytes, invalid[i], 4); assert(!tm_tp_utf8(bytes, 4, false));
    }
}

static void header_integrity(void) {
    uint8_t payload[25], file[1024], damaged[1024];
    int32_t value = -1; TMTPHeader h, previous;
    assert(tm_pack(1, &value, 1, payload, sizeof payload) == 25);
    size_t n = frame(file, (const uint8_t *)single_json, strlen(single_json), payload, 25, 1);
    assert(tm_tp_validate_header(file, n, &h) == 0);
    assert(h.container_bytes == n && h.tensor_count == 1 && h.payload_bytes == 25);
    assert(h.payload_offset == 32 + strlen(single_json));
    for (size_t i = 0; i < n; ++i) for (unsigned bit = 0; bit < 8; ++bit) {
        memcpy(damaged, file, n); damaged[i] ^= (uint8_t)(1U << bit);
        memset(&h, 0xa5, sizeof h); memcpy(&previous, &h, sizeof h);
        assert(tm_tp_validate_header(damaged, n, &h) < 0);
        assert(memcmp(&previous, &h, sizeof h) == 0);
    }
    for (size_t cut = 0; cut < n; ++cut) assert(tm_tp_validate_header(file, cut, &h) < 0);
    assert(tm_tp_validate_header(file, n + 1, &h) == TM_ERR_LENGTH);
    static const unsigned fields[] = {8, 12, 16};
    static const uint64_t bad[] = {1048577, 1025, 67108865};
    for (unsigned i = 0; i < 3; ++i) {
        memcpy(damaged, file, n);
        if (fields[i] == 16) put64(damaged + 16, bad[i]); else put32(damaged + fields[i], (uint32_t)bad[i]);
        assert(tm_tp_validate_header(damaged, n, &h) == TM_ERR_LIMIT);
    }
    uint8_t invalid[] = {0xff}, bom[] = {0xef,0xbb,0xbf,'{','}'};
    n = frame(file, invalid, sizeof invalid, NULL, 0, 0);
    assert(tm_tp_validate_header(file, n, &h) == TM_TP_ERR_TEXT);
    n = frame(file, bom, sizeof bom, NULL, 0, 0);
    assert(tm_tp_validate_header(file, n, &h) == TM_TP_ERR_TEXT);
}

static void descriptors_and_boundaries(void) {
    uint8_t payload[256], file[1024]; int32_t values[8] = {-1,0,0,0,0,0,0,0};
    uint64_t shape[] = {2, 4}; double scales[] = {1.0, 0.25};
    TMTPText axes[] = {{(uint8_t *)"row",3}, {(uint8_t *)"column",6}};
    TMTPValidation result, previous;
    for (int codec = 0; codec < 6; ++codec) {
        size_t inner = (size_t)tm_pack(codec, values, 8, payload, sizeof payload);
        TMTensorDescriptor d = {.name=(uint8_t *)"w",.name_size=1,.shape=shape,.rank=2,
            .scales=scales,.scale_count=2,.scale_axis=0,.axes=axes,.axes_count=2,
            .codec=codec,.offset=0,.length=inner};
        /* Descriptor validation deliberately does not bind to this JSON. */
        size_t n = frame(file, (const uint8_t *)"{}", 2, payload, inner, 1);
        assert(tm_tp_validate_descriptors(file,n,&d,1,8,&result) == 0);
        assert(result.total_trits == 8 && result.framing_valid && result.descriptors_valid);
        assert(!result.json_binding_valid);
        memset(&result,0x55,sizeof result); memcpy(&previous,&result,sizeof result);
        assert(tm_tp_validate_descriptors(file,n,&d,1,7,&result) == TM_ERR_LIMIT);
        assert(memcmp(&result,&previous,sizeof result)==0);
        assert(tm_tp_validate_descriptors(file,n,&d,1,0,&result) == TM_ERR_LIMIT);
        assert(tm_tp_validate_descriptors(file,n,&d,0,8,&result) == TM_TP_ERR_DESCRIPTOR);
        d.offset=UINT64_MAX;
        assert(tm_tp_validate_descriptors(file,n,&d,1,8,&result) == TM_TP_ERR_OFFSET); d.offset=0;
        ++d.length; assert(tm_tp_validate_descriptors(file,n,&d,1,8,&result) == TM_TP_ERR_OFFSET); --d.length;
        file[n-1] ^= 1; reseal_outer(file,2,inner);
        assert(tm_tp_validate_descriptors(file,n,&d,1,8,&result) == TM_ERR_CHECKSUM);
    }
    TMTensorDescriptor d = {.name=(uint8_t *)"w",.name_size=1,.shape=shape,.rank=2,
        .scales=scales,.scale_count=2,.scale_axis=0,.axes=axes,.axes_count=2,.codec=1};
    assert(tm_tp_descriptor_count(&d)==8);
    d.name_size=0; assert(tm_tp_descriptor_count(&d)==TM_TP_ERR_TEXT); d.name_size=1;
    d.name_size=257; assert(tm_tp_descriptor_count(&d)==TM_TP_ERR_TEXT); d.name_size=1;
    d.name=(uint8_t *)"\n"; assert(tm_tp_descriptor_count(&d)==TM_TP_ERR_TEXT); d.name=(uint8_t *)"w";
    d.rank=17; assert(tm_tp_descriptor_count(&d)==TM_TP_ERR_SHAPE); d.rank=2;
    uint64_t dimension=shape[0];
    uint64_t bad_dimensions[]={0,2147483648,UINT64_MAX,4194304};
    for(size_t i=0;i<sizeof bad_dimensions/sizeof bad_dimensions[0];++i){
        shape[0]=bad_dimensions[i]; assert(tm_tp_descriptor_count(&d)==TM_TP_ERR_SHAPE);
    }
    shape[0]=dimension;
    d.axes_count=1; assert(tm_tp_descriptor_count(&d)==TM_TP_ERR_AXES);d.axes_count=2;
    TMTPText saved=axes[1];axes[1]=axes[0];assert(tm_tp_descriptor_count(&d)==TM_TP_ERR_AXES);axes[1]=saved;
    axes[1].size=65;assert(tm_tp_descriptor_count(&d)==TM_TP_ERR_AXES);axes[1]=saved;
    axes[1].size=0;assert(tm_tp_descriptor_count(&d)==TM_TP_ERR_AXES);axes[1]=saved;
    axes[1].data=(uint8_t *)"\xff";axes[1].size=1;assert(tm_tp_descriptor_count(&d)==TM_TP_ERR_AXES);axes[1]=saved;
    int bad_axes[]={-2,2,INT32_MAX};
    for(size_t i=0;i<sizeof bad_axes/sizeof bad_axes[0];++i){d.scale_axis=bad_axes[i];assert(tm_tp_descriptor_count(&d)==TM_TP_ERR_SCALE);}
    d.scale_axis=0;d.scale_count=1;assert(tm_tp_descriptor_count(&d)==TM_TP_ERR_SCALE);d.scale_count=2;
    double bad_scales[]={0.0,-0.0,-1.0,INFINITY,-INFINITY,NAN};
    for(size_t i=0;i<sizeof bad_scales/sizeof bad_scales[0];++i){scales[1]=bad_scales[i];assert(tm_tp_descriptor_count(&d)==TM_TP_ERR_SCALE);}
    scales[1]=DBL_MAX;assert(tm_tp_descriptor_count(&d)==8);
    scales[1]=DBL_TRUE_MIN;assert(tm_tp_descriptor_count(&d)==8);scales[1]=0.25;
    d.codec=99;assert(tm_tp_descriptor_count(&d)==TM_ERR_CODEC);d.codec=1;
    d.rank=0;d.axes_count=0;d.scale_count=1;d.scale_axis=-1;
    assert(tm_tp_descriptor_count(&d)==1);
    d.scale_axis=0;assert(tm_tp_descriptor_count(&d)==TM_TP_ERR_SCALE);
}

static void mixed_order_empty_and_limits(void) {
    uint8_t payload[128],file[1024];int32_t v=-1;uint64_t shape=1;double scale=1.0;
    assert(tm_pack(1,&v,1,payload,sizeof payload)==25);
    assert(tm_pack(0,&v,1,payload+25,sizeof payload-25)==25);
    TMTensorDescriptor descriptors[2]={
        {.name=(uint8_t *)"a",.name_size=1,.shape=&shape,.rank=1,.scales=&scale,.scale_count=1,.scale_axis=-1,.codec=1,.offset=0,.length=25},
        {.name=(uint8_t *)"b",.name_size=1,.rank=0,.scales=&scale,.scale_count=1,.scale_axis=-1,.codec=0,.offset=25,.length=25}
    };
    TMTPValidation r;
    size_t n=frame(file,(const uint8_t *)"{}",2,payload,50,2);
    assert(tm_tp_validate_descriptors(file,n,descriptors,2,2,&r)==0 && r.total_trits==2);
    descriptors[1].name=(uint8_t *)"a";assert(tm_tp_validate_descriptors(file,n,descriptors,2,2,&r)==TM_TP_ERR_DESCRIPTOR);descriptors[1].name=(uint8_t *)"b";
    descriptors[1].offset=0;assert(tm_tp_validate_descriptors(file,n,descriptors,2,2,&r)==TM_TP_ERR_OFFSET);
    descriptors[1].offset=26;assert(tm_tp_validate_descriptors(file,n,descriptors,2,2,&r)==TM_TP_ERR_OFFSET);descriptors[1].offset=25;
    assert(tm_tp_validate_descriptors(file,n,descriptors,2,1,&r)==TM_ERR_LIMIT);
    /* Aggregate bound wins before scanning even correctly resealed invalid inner codes. */
    file[34+24]=255;reseal_inner(file+34,25);reseal_outer(file,2,50);
    assert(tm_tp_validate_descriptors(file,n,descriptors,2,1,&r)==TM_ERR_LIMIT);
    assert(tm_tp_validate_descriptors(file,n,descriptors,2,2,&r)==TM_ERR_CODE);
    n=frame(file,(const uint8_t *)"{}",2,payload,51,2);
    assert(tm_tp_validate_descriptors(file,n,descriptors,2,2,&r)==TM_TP_ERR_OFFSET);
    n=frame(file,(const uint8_t *)"{\"order\":\"C\",\"tensors\":[]}",26,NULL,0,0);
    assert(tm_tp_validate_descriptors(file,n,NULL,0,1,&r)==0 && r.total_trits==0 && !r.json_binding_valid);
    /* Explicit limitation test: valid UTF-8 that IS NOT JSON still passes framing.
     * The flags must never misrepresent that as a complete TensorPack decode. */
    n=frame(file,(const uint8_t *)"not JSON",8,NULL,0,0);
    assert(tm_tp_validate_descriptors(file,n,NULL,0,1,&r)==0 && !r.json_binding_valid);
}

static void nested_canonical_and_count(void) {
    uint8_t payload[128],file[1024];int32_t v=0;uint64_t shape=1;double scale=1.0;
    TMTensorDescriptor d={.name=(uint8_t *)"w",.name_size=1,.shape=&shape,.rank=1,
        .scales=&scale,.scale_count=1,.scale_axis=-1};TMTPValidation r;
    for(int codec=0;codec<6;++codec){
        d.codec=codec;d.length=(uint64_t)tm_pack(codec,&v,1,payload,sizeof payload);
        size_t n=frame(file,(const uint8_t *)single_json,strlen(single_json),payload,(size_t)d.length,1);
        assert(tm_tp_validate_descriptors(file,n,&d,1,1,&r)==0);
        size_t start=32+strlen(single_json);
        int32_t padded[22]={0};padded[tm_group_size(codec)-1]=1;
        assert(tm_encode(codec,padded,tm_group_size(codec),file+start+24,64)>0);
        reseal_inner(file+start,(size_t)d.length);reseal_outer(file,strlen(single_json),(size_t)d.length);
        assert(tm_tp_validate_descriptors(file,n,&d,1,1,&r)==TM_ERR_PADDING);
        if(tm_group_bits(codec)%8){
            memcpy(file+start,payload,(size_t)d.length);file[n-1]|=128;
            reseal_inner(file+start,(size_t)d.length);reseal_outer(file,strlen(single_json),(size_t)d.length);
            assert(tm_tp_validate_descriptors(file,n,&d,1,1,&r)==TM_ERR_PADDING);
        }
        memcpy(file+start,payload,(size_t)d.length);put64(file+start+8,2);
        reseal_inner(file+start,(size_t)d.length);reseal_outer(file,strlen(single_json),(size_t)d.length);
        assert(tm_tp_validate_descriptors(file,n,&d,1,1,&r)==TM_TP_ERR_DESCRIPTOR);
    }
}

int main(void){
    utf8_contract();header_integrity();descriptors_and_boundaries();
    mixed_order_empty_and_limits();nested_canonical_and_count();
    puts("PASS native TensorPack framing/descriptors: UTF8,shape,scales,axes,all-codec nestedTMEM,CRC,offsets,limits; JSON binding explicitly NOT implemented");
    return 0;
}
