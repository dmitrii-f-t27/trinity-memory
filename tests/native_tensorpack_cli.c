/* Native TensorPack CLI contracts. OS/file dispatch is tested separately. */
#include <assert.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
double tm_json_strtod(uint8_t *);
size_t tm_float_shortest(double,uint8_t*,size_t);
#include "codecs.h"
#include "container.h"
#include "tensorpack.h"
#include "json.h"
#include "tensorpack_json.h"
#include "tensorpack_cli.h"
static TMJsonToken tokens[1024];static uint8_t arena[8192];
static TMTensorDescriptor descriptors[16];static uint64_t shapes[256];
static double scales[256];static TMTPText axes[256];
static TMTPJSONWorkspace workspace(void) {
    return (TMTPJSONWorkspace){tokens,1024,arena,sizeof arena,descriptors,16,shapes,256,scales,256,axes,256};
}
int main(void) {
    static const char minimal[]="[{\"name\":\"w\",\"shape\":[3],\"values\":[1,0,-1]}]";
    static const char mixed[]="[{\"name\":\"matrix\",\"shape\":[2],\"values\":[-1,1],\"scales\":[0.5,2.0],\"scale_axis\":0,\"axes\":[\"rows\"]},{\"name\":\"scalar\",\"shape\":[],\"values\":[0],\"codec\":\"sparse41\"}]";
    uint8_t metadata[4096],file[8192],text[8192],old[8192];int32_t values[64];
    TMTPJSONWorkspace w=workspace();TMTPValidation r;
    int64_t n=tm_tp_encode_input_json((uint8_t*)minimal,strlen(minimal),&w,values,64,metadata,sizeof metadata,file,sizeof file);
    assert(n>0);
    assert(tm_tp_decode_json(file,(size_t)n,&w,values,64,64,&r)==0);
    assert(r.total_trits==3&&w.items[0].codec==1&&w.items[0].scale_count==1&&w.items[0].scales[0]==1.0&&w.items[0].scale_axis==-1&&w.items[0].axes_count==0);
    assert(values[0]==1&&values[1]==0&&values[2]==-1);
    int64_t bytes=tm_tp_inspect_json(file,(size_t)n,&w,64,text,sizeof text-1);
    assert(bytes>0);text[bytes]=0;
    assert(strstr((char*)text,"\"format\":\"TensorPack\"")&&strstr((char*)text,"\"total_count\":3")&&strstr((char*)text,"\"count\":3")&&strstr((char*)text,"\"header_bytes\":32"));
    bytes=tm_tp_export_json(file,(size_t)n,&w,values,64,64,text,sizeof text-1);
    assert(bytes>0);text[bytes]=0;
    assert(strstr((char*)text,"\"values\":[1,0,-1]")&&strstr((char*)text,"\"scales\":[1.0]"));
    n=tm_tp_encode_input_json((uint8_t*)mixed,strlen(mixed),&w,values,64,metadata,sizeof metadata,file,sizeof file);
    assert(n>0&&tm_tp_decode_json(file,(size_t)n,&w,values,64,64,&r)==0);
    assert(r.tensor_count==2&&r.total_trits==3&&w.items[0].scale_axis==0&&w.items[0].scales[1]==2.0&&w.items[1].rank==0&&w.items[1].codec==4);
    assert(tm_tp_inspect_json(file,(size_t)n,&w,64,text,1)==TM_ERR_CAPACITY);
    assert(tm_tp_export_json(file,(size_t)n,&w,values,64,64,text,1)==TM_ERR_CAPACITY);
    static const char *bad[]={
        "null","{}","[{}]","[{\"name\":\"w\",\"shape\":[1]}]",
        "[{\"name\":\"w\",\"shape\":[1],\"values\":[1],\"extra\":0}]",
        "[{\"name\":\"w\",\"shape\":[1],\"values\":[1.0]}]",
        "[{\"name\":\"w\",\"shape\":[1],\"values\":[true]}]",
        "[{\"name\":\"w\",\"shape\":[1],\"values\":[2]}]",
        "[{\"name\":\"w\",\"shape\":[1],\"values\":[1],\"scales\":[1]}]",
        "[{\"name\":\"w\",\"shape\":[2],\"values\":[1,1],\"codec\":\"sparse41\"}]",
        "[{\"name\":\"w\",\"shape\":[0],\"values\":[]}]",
        "[{\"name\":\"w\",\"shape\":[1],\"values\":[1],\"shape\":[1]}]"
    };
    for(size_t i=0;i<sizeof bad/sizeof bad[0];++i){
        memset(file,0xa5,sizeof file);memcpy(old,file,sizeof file);
        assert(tm_tp_encode_input_json((uint8_t*)bad[i],strlen(bad[i]),&w,values,64,metadata,sizeof metadata,file,sizeof file)<0);
        assert(memcmp(file,old,sizeof file)==0);
    }
    assert(tm_tp_encode_input_json((uint8_t*)"[]",2,&w,values,64,metadata,sizeof metadata,file,sizeof file)==58);
    puts("PASS native TensorPack CLI mapper: required fields, defaults, typed values, inspect/export schema, malformed-input atomic outputs");
    return 0;
}
