/* Independent native JSON/parser writer acceptance; no Python runtime.
 * Link native/float.cpp, the generic locale-stable strtod adapter. */
#include <assert.h>
#include <float.h>
#include <inttypes.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
double tm_json_strtod(uint8_t *nul_terminated);
size_t tm_float_shortest(double, uint8_t *, size_t);
#include "json.h"
#include "json_writer.h"

static TMJsonToken tokens[1024];
static uint8_t arena[8192];
static TMJsonResult result;
static int parse_n(const uint8_t *s,size_t n,size_t depth){
    return tm_json_parse((uint8_t *)s,n,tokens,1024,arena,sizeof arena,depth,&result);
}
static int parse(const char *s){return parse_n((const uint8_t *)s,strlen(s),8);}
static TMJsonToken *root_token(void){return &tokens[result.root];}
static void token_equals(TMJsonToken *t,const uint8_t *s,size_t n){
    assert(t->text_size==n && memcmp(t->text,s,n)==0 && t->text[n]==0);
}
static void grammar_and_tree(void){
    assert(parse(" \r\n\t{\"array\":[null,false,true,12,-3,4.5,\"ok\",{},[]],\"nested\":{\"x\":1}} \t")==0);
    assert(root_token()->kind==TM_JSON_OBJECT);
    int64_t a=tm_json_field(tokens,result.token_count,result.root,(uint8_t *)"array",5);
    assert(a>=0 && tokens[a].kind==TM_JSON_ARRAY);
    int kinds[]={TM_JSON_NULL,TM_JSON_FALSE,TM_JSON_TRUE,TM_JSON_INTEGER,TM_JSON_INTEGER,
        TM_JSON_REAL,TM_JSON_STRING,TM_JSON_OBJECT,TM_JSON_ARRAY};
    int64_t child=tokens[a].first;
    for(size_t i=0;i<sizeof kinds/sizeof kinds[0];++i){assert(child>=0 && tokens[child].kind==kinds[i]);child=tokens[child].next;}
    assert(child==-1);
    int64_t nested=tm_json_field(tokens,result.token_count,result.root,(uint8_t *)"nested",6);
    int64_t x=tm_json_field(tokens,result.token_count,nested,(uint8_t *)"x",1);
    assert(x>=0 && tokens[x].integer==1);
    assert(tm_json_field(tokens,result.token_count,nested,(uint8_t *)"missing",7)==-1);
    assert(tm_json_field(tokens,result.token_count,-1,(uint8_t *)"x",1)==-2);
    assert(tm_json_field(tokens,result.token_count,x,(uint8_t *)"x",1)==-2);
    assert(parse("[{},{}]")==0);
    assert(parse("{\"a\":{\"a\":1},\"b\":{\"a\":2}}")==0);
    const char *bad[]={""," ","{","[","[1,]","{\"a\":1,}","{\"a\" 1}","{\"a\":}","{1:2}",
        "[1 2]","{\"a\":1 \"b\":2}","[}","{]","null x","nullnull","TRUE","False","None",
        "undefined","NaN","Infinity","-Infinity","//comment\n0","/*x*/0","[truefalse]","[1true]",
        "\"raw\nnewline\"","\"\\x41\"","\"\\uZZZZ\"","\"unfinished","\"\\","\"bad\\a\"","0\v"};
    for(size_t i=0;i<sizeof bad/sizeof bad[0];++i)assert(parse(bad[i])<0);
    const char *complete="{\"one\":[1,true,\"text\"],\"two\":null}";
    for(size_t i=0;i<strlen(complete);++i)assert(parse_n((uint8_t *)complete,i,8)<0);
    const uint8_t bom[]={0xef,0xbb,0xbf,'{','}'};assert(parse_n(bom,sizeof bom,8)<0);
    const uint8_t zero[]={0};assert(parse_n(zero,1,8)<0);
}
static void strings_unicode_duplicates(void){
    assert(parse("\"\\\"\\\\\\/\\b\\f\\n\\r\\t\\u0000\\u007f\"")==0);
    const uint8_t escaped[]={34,92,47,8,12,10,13,9,0,127};
    token_equals(root_token(),escaped,sizeof escaped);
    assert(parse("\"\\u0061\\u00e9\\u20ac\\ud83d\\ude00\"")==0);
    const uint8_t unicode[]={'a',0xc3,0xa9,0xe2,0x82,0xac,0xf0,0x9f,0x98,0x80};
    token_equals(root_token(),unicode,sizeof unicode);
    uint8_t raw[sizeof unicode+2];raw[0]=34;memcpy(raw+1,unicode,sizeof unicode);raw[sizeof raw-1]=34;
    assert(parse_n(raw,sizeof raw,8)==0);token_equals(root_token(),unicode,sizeof unicode);
    const char *dup[]={"{\"a\":1,\"a\":2}","{\"a\":1,\"\\u0061\":2}",
        "{\"\\ud83d\\ude00\":1,\"😀\":2}","{\"a\\u0000b\":1,\"a\\u0000b\":2}",
        "{\"x\":{\"key\":null,\"\\u006bey\":false}}"};
    for(size_t i=0;i<sizeof dup/sizeof dup[0];++i)assert(parse(dup[i])==TM_JSON_ERR_DUPLICATE);
    const char *surrogates[]={"\"\\ud800\"","\"\\udfff\"","\"\\ud800x\"","\"\\ud800\\u0000\"",
        "\"\\udc00\\ud800\"","\"\\ud800\\ud800\""};
    for(size_t i=0;i<sizeof surrogates/sizeof surrogates[0];++i)assert(parse(surrogates[i])<0);
    static const uint8_t invalid[][6]={{34,0xc0,0x80,34,0,0},{34,0xc1,0xbf,34,0,0},
        {34,0xed,0xa0,0x80,34,0},{34,0xe0,0x9f,0xbf,34,0},{34,0xf0,0x8f,0xbf,0xbf,34},
        {34,0xf4,0x90,0x80,0x80,34},{34,0xf5,0x80,0x80,0x80,34},{34,0xff,34,0,0,0},
        {34,0xe2,34,0,0,0},{34,0x80,34,0,0,0}};
    for(size_t i=0;i<sizeof invalid/sizeof invalid[0];++i)assert(parse_n(invalid[i],6,8)<0);
    /* Sample Unicode scalars at a fixed stride: raw versus JSON escape spelling. */
    for(uint32_t cp=0;cp<=0x10ffff;cp+=127){
        if(cp>=0xd800 && cp<=0xdfff)continue;
        char text[32];
        if(cp<=0xffff)snprintf(text,sizeof text,"\"\\u%04x\"",cp);
        else{uint32_t q=cp-0x10000;snprintf(text,sizeof text,"\"\\u%04x\\u%04x\"",0xd800+(q>>10),0xdc00+(q&1023));}
        assert(parse(text)==0);
        uint8_t bytes[8];size_t n=root_token()->text_size;memcpy(bytes,root_token()->text,n);
        if(cp<32 || cp==34 || cp==92)continue;
        uint8_t quoted[10];quoted[0]=34;memcpy(quoted+1,bytes,n);quoted[n+1]=34;
        assert(parse_n(quoted,n+2,8)==0);token_equals(root_token(),bytes,n);
    }
}
static void exact_numbers(void){
    assert(parse("18446744073709551615")==0 && root_token()->uinteger==UINT64_MAX);
    assert(!root_token()->negative && !root_token()->integer_fits_i64);
    assert(parse("9223372036854775807")==0 && root_token()->integer==INT64_MAX && root_token()->integer_fits_i64);
    assert(parse("-9223372036854775808")==0 && root_token()->integer==INT64_MIN && root_token()->negative);
    assert(parse("-0")==0 && root_token()->integer==0 && !root_token()->negative);
    assert(parse("1")==0 && root_token()->kind==TM_JSON_INTEGER);
    assert(parse("1.0")==0 && root_token()->kind==TM_JSON_REAL && root_token()->real==1.0);
    assert(parse("1e0")==0 && root_token()->kind==TM_JSON_REAL);
    assert(parse("-0.0")==0 && signbit(root_token()->real));
    assert(parse("1.7976931348623157e308")==0 && root_token()->real==DBL_MAX);
    assert(parse("4.9406564584124654e-324")==0 && root_token()->real==DBL_TRUE_MIN);
    assert(parse("1e-999")==0 && root_token()->real==0.0);
    const char *bad[]={"01","-01","+1",".1","1.","1e","1e+","1e-","--1","0x1","1_0",
        "18446744073709551616","-9223372036854775809","100000000000000000000","1e999","-1e999"};
    for(size_t i=0;i<sizeof bad/sizeof bad[0];++i)assert(parse(bad[i])<0);
    char text[64];
    for(int64_t i=-10000;i<10000;i+=13){snprintf(text,sizeof text,"%" PRId64,i);assert(parse(text)==0 && root_token()->integer==i);}
}
static void limits_and_fuzz(void){
    assert(parse_n((uint8_t *)"[[[[[[[[0]]]]]]]]",17,8)==0);
    assert(parse_n((uint8_t *)"[[[[[[[[[0]]]]]]]]]",19,8)==TM_JSON_ERR_DEPTH);
    assert(parse_n((uint8_t *)"[]",2,0)==TM_JSON_ERR_DEPTH);
    assert(parse_n((uint8_t *)"[]",2,65)==TM_JSON_ERR_DEPTH);
    TMJsonResult output,previous;memset(&output,0x55,sizeof output);memcpy(&previous,&output,sizeof output);
    assert(tm_json_parse((uint8_t *)"[0]",3,tokens,1,arena,sizeof arena,8,&output)==TM_JSON_ERR_CAPACITY);
    assert(memcmp(&output,&previous,sizeof output)==0);
    assert(tm_json_parse((uint8_t *)"\"a\"",3,tokens,1024,arena,1,8,&output)==TM_JSON_ERR_CAPACITY);
    assert(tm_json_parse((uint8_t *)"\"a\"",3,tokens,1024,arena,2,8,&output)==0);
    assert(tm_json_parse((uint8_t *)"null",4,tokens,1,NULL,0,8,&output)==0);
    assert(tm_json_parse(NULL,0,NULL,0,NULL,0,8,&output)==TM_JSON_ERR_SYNTAX);
    uint32_t random=97;
    for(size_t trial=0;trial<30000;++trial){
        size_t n=trial%127+1;uint8_t *data=malloc(n);assert(data);
        for(size_t i=0;i<n;++i){random=random*1664525U+1013904223U;data[i]=(uint8_t)(random>>24);}
        int status=parse_n(data,n,8);
        if(status==0){assert(result.root>=0 && result.token_count<=1024 && result.arena_used<=sizeof arena);}
        free(data);
    }
    /* Lookup has a bounded failure even if a caller corrupts the token graph. */
    assert(parse("{\"a\":0}")==0);tokens[tokens[0].first].next=tokens[0].first;
    assert(tm_json_field(tokens,result.token_count,0,(uint8_t *)"missing",7)==-2);
}
static void writer_roundtrips(void){
    uint8_t output[4096],text[256];TMJsonWriter w;
    for(unsigned i=0;i<128;++i)text[i]=(uint8_t)i;
    tm_json_writer_init(output,sizeof output,&w);
    assert(tm_json_write_quoted(&w,text,128)==0);
    assert(parse_n(output,w.used,8)==0);token_equals(root_token(),text,128);
    uint8_t unicode[]={0xc3,0xa9,0xe2,0x82,0xac,0xf4,0x8f,0xbf,0xbf};
    tm_json_writer_init(output,sizeof output,&w);assert(tm_json_write_quoted(&w,unicode,sizeof unicode)==0);
    assert(parse_n(output,w.used,8)==0);token_equals(root_token(),unicode,sizeof unicode);
    tm_json_writer_init(output,sizeof output,&w);assert(tm_json_write_u64(&w,UINT64_MAX)==0);
    assert(w.used==20 && memcmp(output,"18446744073709551615",20)==0);
    tm_json_writer_init(output,sizeof output,&w);assert(tm_json_write_i64(&w,INT64_MIN)==0);
    assert(w.used==20 && memcmp(output,"-9223372036854775808",20)==0);
    tm_json_writer_init(output,sizeof output,&w);assert(tm_json_write_i64(&w,0)==0 && w.used==1 && output[0]=='0');
    tm_json_writer_init(output,sizeof output,&w);tm_json_write_byte(&w,'[');
    tm_json_write_bool(&w,true);tm_json_write_byte(&w,',');tm_json_write_bool(&w,false);
    tm_json_write_byte(&w,',');tm_json_write_null(&w);tm_json_write_byte(&w,']');
    assert(w.error==0 && w.used==17 && memcmp(output,"[true,false,null]",17)==0);
    assert(parse_n(output,w.used,8)==0);
    memset(output,0x55,sizeof output);tm_json_writer_init(output,2,&w);
    assert(tm_json_write_quoted(&w,(uint8_t *)"a",1)==TM_JSON_ERR_CAPACITY && w.used==0 && output[0]==0x55);
    assert(tm_json_write_byte(&w,0)==TM_JSON_ERR_CAPACITY && w.used==0);
    tm_json_writer_init(output,sizeof output,&w);uint8_t invalid[]={0xc0,0x80};
    assert(tm_json_write_quoted(&w,invalid,2)==TM_JSON_ERR_UTF8 && w.used==0);
    tm_json_writer_init(output,2,&w);assert(tm_json_write_bytes(&w,(uint8_t *)"abc",3)==TM_JSON_ERR_CAPACITY && w.used==0);
    tm_json_writer_init(output,0,&w);assert(tm_json_write_u64(&w,0)==TM_JSON_ERR_CAPACITY);
}
int main(void){
    grammar_and_tree();strings_unicode_duplicates();exact_numbers();limits_and_fuzz();writer_roundtrips();
    puts("PASS strict native JSON grammar/tree,UTF8/escapes/duplicatekeys,numerictypes/ranges,depth/capacities,30000 bounded-byte fuzz inputs; shared writer roundtrips");
    return 0;
}
