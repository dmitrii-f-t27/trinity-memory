/* Python3.14 random.Random fixtures; source oracle tests persist alongside.
 * Compile generated random.h under ASan/UBSan. No external native libraries. */
#include <assert.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "random.h"
static uint64_t hashword(uint64_t hash, uint64_t value, unsigned bytes) {
    for(unsigned i=0;i<bytes;i++) { hash ^= (value >> (i*8)) & 255; hash *= UINT64_C(1099511628211); }
    return hash;
}
static void test_golden(void) {
    const uint64_t seeds[]={0,27,UINT64_MAX};
    const uint64_t hashes[]={UINT64_C(12642929893523299873),UINT64_C(14286601783693711490),UINT64_C(2545284485755003723)};
    TMRandom state;
    for(size_t n=0;n<3;n++) {
        tm_random_init(&state,seeds[n]);uint64_t h=UINT64_C(14695981039346656037);
        for(unsigned i=0;i<10000;i++)h=hashword(h,tm_random_u32(&state),4);
        assert(h==hashes[n]);
    }
    tm_random_init(&state,27);uint64_t h=UINT64_C(14695981039346656037);
    for(unsigned i=0;i<20;i++)for(unsigned bits=0;bits<=64;bits++)h=hashword(h,tm_random_bits(&state,bits),8);
    assert(h==UINT64_C(11669392145595928792));
    tm_random_init(&state,27);h=UINT64_C(14695981039346656037);int64_t value=0;
    for(unsigned i=0;i<10000;i++){assert(tm_random_randint(&state,INT64_MIN,INT64_MAX,&value)==0);h=hashword(h,(uint64_t)value,8);}
    assert(h==UINT64_C(7007356175710845608));
    tm_random_init(&state,27);h=UINT64_C(14695981039346656037);
    uint32_t out[100],scratch[278];
    const uint32_t pairs[][2]={{4,1},{8,2},{21,5},{22,5},{85,6},{86,6},{277,22},{278,22},{10000,100},{100000,5}};
    for(unsigned repeat=0;repeat<50;repeat++)for(size_t i=0;i<sizeof pairs/sizeof pairs[0];i++){
        uint32_t n=pairs[i][0],k=pairs[i][1];
        assert(tm_random_sample(&state,n,k,out,100,scratch,278)==0);
        for(uint32_t j=0;j<k;j++)h=hashword(h,out[j],4);
    }
    assert(h==UINT64_C(17460686041562154496));
}
static void test_validation(void) {
    TMRandom state,previous,other;tm_random_init(&state,27);previous=state;
    uint64_t bits=123;int64_t value=77;uint32_t out[100],scratch[200];
    memset(out,0xa5,sizeof out);memset(scratch,0xa5,sizeof scratch);
    assert(tm_random_seed_words(&state,NULL,0)==-70);
    assert(tm_random_getrandbits(&state,65,&bits)==-70&&bits==123);
    assert(tm_random_choice(&state,NULL,0,&value)==-70&&value==77);
    assert(tm_random_randint(&state,2,1,&value)==-70&&value==77);
    assert(tm_random_range(&state,0,1,0,&value)==-70&&value==77);
    assert(tm_random_range(&state,0,0,1,&value)==-70&&value==77);
    assert(tm_random_range(&state,0,1,-1,&value)==-70&&value==77);
    assert(tm_random_range(&state,1,0,1,&value)==-70&&value==77);
    assert(tm_random_sample(&state,8,9,out,100,scratch,200)==-70);
    assert(tm_random_sample(&state,8,2,out,1,scratch,200)==-71);
    assert(tm_random_sample(&state,8,2,out,100,scratch,7)==-71);
    assert(tm_random_sample_scratch(UINT32_MAX,UINT32_MAX)==UINT32_MAX);
    assert(tm_random_sample_scratch(0,0)==0);
    assert(tm_random_sample(&state,0,0,NULL,0,NULL,0)==0);
    assert(memcmp(&state,&previous,sizeof state)==0);
    for(size_t i=0;i<100;i++)assert(out[i]==UINT32_C(0xa5a5a5a5));
    for(size_t i=0;i<200;i++)assert(scratch[i]==UINT32_C(0xa5a5a5a5));
    assert(tm_random_getrandbits(&state,0,&bits)==0&&bits==0);
    assert(memcmp(&state,&previous,sizeof state)==0);
    const int64_t signedseeds[]={0,27,-27,INT64_MIN,INT64_MAX};
    for(size_t i=0;i<sizeof signedseeds/sizeof signedseeds[0];i++){
        int64_t seed=signedseeds[i];uint64_t magnitude=seed<0?(uint64_t)(-(seed+1))+1:(uint64_t)seed;
        tm_random_init_i64(&state,seed);tm_random_init(&other,magnitude);
        assert(memcmp(&state,&other,sizeof state)==0);
    }
    uint32_t words[]={27,0,0,0};tm_random_init(&other,27);
    assert(tm_random_seed_words(&state,words,4)==0&&memcmp(&state,&other,sizeof state)==0);
    for(unsigned i=0;i<10000;i++) {
        assert(tm_random_randint(&state,INT64_MIN,INT64_MIN,&value)==0&&value==INT64_MIN);
        assert(tm_random_randint(&state,INT64_MAX,INT64_MAX,&value)==0&&value==INT64_MAX);
        assert(tm_random_range(&state,INT64_MAX,INT64_MIN,INT64_MIN,&value)==0&&(value==INT64_MAX||value==-1));
        assert(tm_random_range(&state,0,INT64_MIN,-1,&value)==0&&value<=0&&value>INT64_MIN);
        assert(tm_random_range(&state,INT64_MIN,INT64_MAX,INT64_MAX,&value)==0&&(value==INT64_MIN||value==-1||value==INT64_MAX-1));
    }
}
int main(void){test_golden();test_validation();puts("native MT19937/Python integer RNG: fixtures, rejection, sample, overflow and capacity checks passed");return 0;}
