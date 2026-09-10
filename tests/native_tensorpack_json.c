/* Native JSON/TensorPack integration acceptance. Generated algorithms only. */
#include <assert.h>
#include <float.h>
#include <math.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
double tm_json_strtod(uint8_t *);
size_t tm_float_shortest(double, uint8_t *, size_t);
#include "codecs.h"
#include "container.h"
#include "tensorpack.h"
#include "json.h"
#include "tensorpack_json.h"

static TMJsonToken tokens[4096];
static uint8_t arena[65536];
static TMTensorDescriptor descriptors[16];
static uint64_t shapes[256];
static double scales[4096];
static TMTPText axes[256];
static TMTPJSONWorkspace workspace(void) {
    return (TMTPJSONWorkspace){tokens,4096,arena,sizeof arena,descriptors,16,
        shapes,256,scales,4096,axes,256};
}
/* CRC oracle has a table, unlike production's bit loop. */
static uint32_t crc(const uint8_t *data, size_t size, uint32_t state) {
    uint32_t table[256];
    for (unsigned i=0;i<256;++i) {
        uint32_t n=i;
        for (unsigned bit=0;bit<8;++bit) n=(n>>1)^((n&1)?UINT32_C(0xedb88320):0);
        table[i]=n;
    }
    for(size_t i=0;i<size;++i) state=table[(state^data[i])&255]^(state>>8);
    return state;
}
static void put32(uint8_t *out,uint32_t x) { for(unsigned i=0;i<4;++i)out[i]=(uint8_t)(x>>(8*i)); }
static void put64(uint8_t *out,uint64_t x) { for(unsigned i=0;i<8;++i)out[i]=(uint8_t)(x>>(8*i)); }
static size_t frame(uint8_t *out,const char *json,const uint8_t *payload,size_t size,unsigned count) {
    size_t metadata=strlen(json);
    memset(out,0,32);memcpy(out,"TTPK",4);out[4]=1;
    put32(out+8,(uint32_t)metadata);put32(out+12,count);put64(out+16,size);
    memcpy(out+32,json,metadata);if(size)memcpy(out+32+metadata,payload,size);
    put32(out+24,crc(out+32,metadata,crc(out,24,UINT32_MAX))^UINT32_MAX);
    put32(out+28,crc(out+32+metadata,size,UINT32_MAX)^UINT32_MAX);
    return 32+metadata+size;
}
static void scalar_roundtrip_and_canonical_bytes(void) {
    uint8_t out[4096], expected[4096], metadata[2048], inner[25];
    int32_t value=-1, restored[4]={77,77,77,77};double scale=1.0;
    TMTensorDescriptor d={.name=(uint8_t*)"w",.name_size=1,.rank=0,
        .scales=&scale,.scale_count=1,.scale_axis=-1,.codec=1};
    static const char canonical[]="{\"order\":\"C\",\"tensors\":[{\"axes\":[],\"codec\":\"dense5\",\"length\":25,\"name\":\"w\",\"offset\":0,\"scale_axis\":null,\"scales\":[1.0],\"shape\":[]}]}";
    assert(tm_pack(1,&value,1,inner,sizeof inner)==25);
    size_t n=frame(expected,canonical,inner,25,1);
    assert(tm_tp_encode_json(&d,1,&value,1,metadata,sizeof metadata,out,sizeof out)==(int64_t)n);
    assert(memcmp(out,expected,n)==0);
    TMTPJSONWorkspace w=workspace();TMTPValidation r;
    assert(tm_tp_decode_json(out,n,&w,restored,4,4194304,&r)==0);
    assert(r.json_binding_valid&&r.tensor_count==1&&r.total_trits==1&&restored[0]==-1&&restored[1]==77);
    assert(w.items[0].rank==0&&w.items[0].scale_axis==-1&&w.items[0].scales[0]==1.0);
    assert(w.items[0].name_size==1&&w.items[0].name[0]=='w');
    for(int codec=0;codec<6;++codec) {
        d.codec=codec;
        int64_t size=tm_tp_encode_json(&d,1,&value,1,metadata,sizeof metadata,out,sizeof out);
        assert(size>32);
        assert(tm_tp_decode_json(out,(size_t)size,&w,restored,4,1,&r)==0);
        assert(w.items[0].codec==codec&&restored[0]==value&&r.json_binding_valid);
    }
    n=(size_t)tm_tp_encode_json(NULL,0,NULL,0,metadata,sizeof metadata,out,sizeof out);
    assert(n==58);
    assert(memcmp(out+32,"{\"order\":\"C\",\"tensors\":[]}",26)==0);
    assert(tm_tp_decode_json(out,n,&w,NULL,0,1,&r)==0&&r.total_trits==0&&r.tensor_count==0);
}
static void semantic_binding_errors(void) {
    static const char *bad[]={
        "{}", "[]", "null", "{\"order\":\"F\",\"tensors\":[]}",
        "{\"order\":\"C\",\"tensors\":[],\"extra\":0}",
        "{\"order\":\"C\",\"order\":\"C\",\"tensors\":[]}",
        "{\"order\":\"C\",\"tensors\":[]}",
        "{\"order\":\"C\",\"tensors\":[{}]}",
        "{\"order\":\"C\",\"tensors\":[{\"name\":\"w\",\"shape\":[],\"scales\":[1],\"scale_axis\":null,\"axes\":[],\"codec\":\"dense5\",\"offset\":0,\"length\":25}]}",
        "{\"order\":\"C\",\"tensors\":[{\"name\":\"w\",\"shape\":[0],\"scales\":[1.0],\"scale_axis\":null,\"axes\":[],\"codec\":\"dense5\",\"offset\":0,\"length\":25}]}",
        "{\"order\":\"C\",\"tensors\":[{\"name\":\"w\",\"shape\":[true],\"scales\":[1.0],\"scale_axis\":null,\"axes\":[],\"codec\":\"dense5\",\"offset\":0,\"length\":25}]}",
        "{\"order\":\"C\",\"tensors\":[{\"name\":\"w\",\"shape\":[1.0],\"scales\":[1.0],\"scale_axis\":null,\"axes\":[],\"codec\":\"dense5\",\"offset\":0,\"length\":25}]}",
        "{\"order\":\"C\",\"tensors\":[{\"name\":\"w\",\"shape\":[],\"scales\":[1.0],\"scale_axis\":null,\"axes\":[],\"codec\":\"baseline2\",\"offset\":0,\"length\":25}]}",
        "{\"order\":\"C\",\"tensors\":[{\"name\":\"w\",\"shape\":[],\"scales\":[1.0],\"scale_axis\":null,\"axes\":[],\"codec\":\"dense5\",\"offset\":0.0,\"length\":25}]}",
        "{\"order\":\"C\",\"tensors\":[{\"name\":\"w\",\"shape\":[],\"scales\":[1.0],\"scale_axis\":null,\"axes\":[],\"codec\":\"dense5\",\"offset\":1,\"length\":25}]}",
        "{\"order\":\"C\",\"tensors\":[{\"name\":\"w\",\"shape\":[],\"scales\":[NaN],\"scale_axis\":null,\"axes\":[],\"codec\":\"dense5\",\"offset\":0,\"length\":25}]}",
        "{\"order\":\"C\",\"tensors\":[{\"name\":\"w\",\"shape\":[],\"scales\":[1e309],\"scale_axis\":null,\"axes\":[],\"codec\":\"dense5\",\"offset\":0,\"length\":25}]}",
        "{\"order\":\"C\",\"tensors\":[{\"name\":\"w\",\"shape\":[],\"scales\":[1e-999],\"scale_axis\":null,\"axes\":[],\"codec\":\"dense5\",\"offset\":0,\"length\":25}]}",
        "{\"order\":\"C\",\"tensors\":[{\"name\":\"w\",\"shape\":[],\"scales\":[1.0],\"scale_axis\":0,\"axes\":[],\"codec\":\"dense5\",\"offset\":0,\"length\":25}]}",
        "{\"order\":\"C\",\"tensors\":[{\"name\":\"\\u0000\",\"shape\":[],\"scales\":[1.0],\"scale_axis\":null,\"axes\":[],\"codec\":\"dense5\",\"offset\":0,\"length\":25}]}"
    };
    uint8_t inner[25],file[4096];int32_t value=1,output[4],old[4];
    assert(tm_pack(1,&value,1,inner,sizeof inner)==25);
    TMTPJSONWorkspace w=workspace();TMTPValidation r,previous;
    for(size_t i=0;i<sizeof bad/sizeof bad[0];++i) {
        size_t n=frame(file,bad[i],inner,25,1);
        memset(output,0x55,sizeof output);memcpy(old,output,sizeof old);
        memset(&r,0x66,sizeof r);memcpy(&previous,&r,sizeof r);
        assert(tm_tp_decode_json(file,n,&w,output,4,4194304,&r)<0);
        assert(memcmp(output,old,sizeof output)==0&&memcmp(&r,&previous,sizeof r)==0);
    }
    static const char valid[]="{\"tensors\":[{\"shape\":[],\"name\":\"\\u0077\",\"scales\":[1e0],\"scale_axis\":null,\"axes\":[],\"codec\":\"dense5\",\"offset\":-0,\"length\":25}],\"order\":\"C\"}";
    size_t n=frame(file,valid,inner,25,1);
    assert(tm_tp_decode_json(file,n,&w,output,4,1,&r)==0&&r.json_binding_valid&&output[0]==1);
    assert(w.items[0].name[0]=='w');
    assert(tm_tp_decode_json(file,n,&w,output,0,1,&r)==TM_ERR_CAPACITY);
    assert(tm_tp_decode_json(file,n,&w,output,4,0,&r)==TM_ERR_LIMIT);
    TMTPJSONWorkspace small=w;small.token_capacity=2;
    assert(tm_tp_decode_json(file,n,&small,output,4,1,&r)<0);
    small=w;small.arena_capacity=1;assert(tm_tp_decode_json(file,n,&small,output,4,1,&r)<0);
    small=w;small.item_capacity=0;assert(tm_tp_decode_json(file,n,&small,output,4,1,&r)==TM_ERR_CAPACITY);
    small=w;small.scale_capacity=0;assert(tm_tp_decode_json(file,n,&small,output,4,1,&r)==TM_ERR_CAPACITY);
}
static void multiple_tensors_unicode_and_encoder_errors(void) {
    uint8_t file[8192],old[8192],metadata[4096];
    int32_t values[]={-1,0,1,-1};uint64_t shape=3;double scale[]={1e-5,1e16,DBL_TRUE_MIN},one=1.0;
    TMTPText axis={(uint8_t*)"sample",6};
    static const uint8_t name[]={0xf0,0x9f,0xa7,0xa0,'"','\\'};
    TMTensorDescriptor input[]={
        {.name=(uint8_t*)name,.name_size=sizeof name,.shape=&shape,.rank=1,.scales=scale,.scale_count=3,.scale_axis=0,.axes=&axis,.axes_count=1,.codec=1},
        {.name=(uint8_t*)"scalar",.name_size=6,.rank=0,.scales=&one,.scale_count=1,.scale_axis=-1,.codec=4}
    };
    int64_t n=tm_tp_encode_json(input,2,values,4,metadata,sizeof metadata,file,sizeof file);
    assert(n>0);
    int32_t output[4];TMTPJSONWorkspace w=workspace();TMTPValidation r;
    assert(tm_tp_decode_json(file,(size_t)n,&w,output,4,4,&r)==0);
    assert(r.json_binding_valid&&r.tensor_count==2&&r.total_trits==4&&memcmp(output,values,sizeof values)==0);
    assert(w.items[0].name_size==sizeof name&&memcmp(w.items[0].name,name,sizeof name)==0);
    assert(w.items[0].rank==1&&w.items[0].shape[0]==3&&w.items[0].scales[2]==DBL_TRUE_MIN);
    assert(w.items[1].rank==0&&w.items[1].offset==25&&w.items[1].codec==4);
    assert(tm_tp_decode_json(file,(size_t)n,&w,output,4,3,&r)==TM_ERR_LIMIT);
    memset(file,0xa5,sizeof file);memcpy(old,file,sizeof file);
    assert(tm_tp_encode_json(input,2,values,4,metadata,1,file,sizeof file)==TM_ERR_CAPACITY);
    assert(memcmp(file,old,sizeof file)==0);
    assert(tm_tp_encode_json(input,2,values,4,metadata,sizeof metadata,file,1)==TM_ERR_CAPACITY);
    assert(memcmp(file,old,sizeof file)==0);
    values[3]=2;assert(tm_tp_encode_json(input,2,values,4,metadata,sizeof metadata,file,sizeof file)==TM_ERR_TRIT);
    assert(memcmp(file,old,sizeof file)==0);values[3]=-1;
    input[1].name=input[0].name;input[1].name_size=input[0].name_size;
    assert(tm_tp_encode_json(input,2,values,4,metadata,sizeof metadata,file,sizeof file)==TM_TP_ERR_DESCRIPTOR);
    assert(memcmp(file,old,sizeof file)==0);
    input[1].name=(uint8_t*)"scalar";input[1].name_size=6;
    scale[0]=NAN;assert(tm_tp_encode_json(input,2,values,4,metadata,sizeof metadata,file,sizeof file)==TM_TP_ERR_SCALE);
    assert(memcmp(file,old,sizeof file)==0);scale[0]=1.0;
    input[0].codec=4;assert(tm_tp_encode_json(input,2,values,4,metadata,sizeof metadata,file,sizeof file)==TM_ERR_SPARSITY);
    assert(memcmp(file,old,sizeof file)==0);
}
static void float_presentation(void) {
    static const double inputs[]={1.0,0.0001,0.00001,100000.0,1e15,1e16,1e20,DBL_TRUE_MIN,DBL_MAX,0.1};
    static const char *expected[]={"1.0","0.0001","1e-05","100000.0","1000000000000000.0","1e+16","1e+20","5e-324","1.7976931348623157e+308","0.1"};
    for(size_t i=0;i<sizeof inputs/sizeof inputs[0];++i){
        uint8_t text[128];TMTPJSONWriter w={text,sizeof text,0,0};
        tm_tpj_float(&w,inputs[i]);assert(w.status==0);
        assert(w.size==strlen(expected[i])&&memcmp(text,expected[i],w.size)==0);
    }
    uint8_t text[128];
    int64_t n=tm_tpj_format_f64(0x1.17dccc80c1ae8p+64,text,sizeof text);
    const char *shortest="2.0166218084835557e+19";
    assert(n==(int64_t)strlen(shortest)&&memcmp(text,shortest,(size_t)n)==0);
    assert(tm_tpj_format_f64(-0.0,text,sizeof text)==4&&memcmp(text,"-0.0",4)==0);
    assert(tm_tpj_format_f64(0.0,text,sizeof text)==3&&memcmp(text,"0.0",3)==0);
    assert(tm_tpj_format_f64(-0.125,text,sizeof text)==6&&memcmp(text,"-0.125",6)==0);
    assert(tm_tpj_format_f64(NAN,text,sizeof text)<0);
    assert(tm_tpj_format_f64(INFINITY,text,sizeof text)<0);
}
int main(void) {
    test_tm_tpj_empty_pack_contract();test_tm_tpj_float_threshold_contract();
    scalar_roundtrip_and_canonical_bytes();semantic_binding_errors();
    multiple_tensors_unicode_and_encoder_errors();float_presentation();
    puts("PASS full native TensorPack JSON encode/decode: canonical bytes, all codecs, typed fields, Unicode, scalar/empty/multi-tensor, limits and atomic error outputs");
    return 0;
}
