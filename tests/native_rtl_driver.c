/* Native RTL packet/file and observed-output checks. The separate integration
 * test invokes real Icarus; parsing a fixture alone is not simulation evidence. */
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "process.h"
#include "rtl_driver.h"
static void preparation(void) {
    int32_t w[]={1,-1,0,1,-1,1},a[]={-128,127,0,127,-128,1};
    uint64_t packets[3]={77,88,99},encoded[1]={77};TMRtlExpected expected={77,true};
    assert(tm_rtl_prepare_dot(1,w,a,6,packets,2,&expected)==2&&expected.result==1&&!expected.error);
    assert((packets[0]&1023)==(2+0*3+1*9+2*27+0*81));
    assert((packets[0]>>50)==31);
    assert((packets[1]>>50)==33&&((packets[1]>>10)&255)==1);
    assert((packets[1]&1023)==122&&packets[2]==99);
    assert(tm_rtl_prepare_dot(0,w,a,6,packets,2,&expected)==2);
    assert((packets[0]&1023)==(1|(2<<2)|(1<<6)|(2<<8)));
    assert((packets[1]&1023)==1);
    assert(tm_rtl_prepare_dot(1,NULL,NULL,0,packets,1,&expected)==1&&expected.result==0);
    assert(packets[0]==(UINT64_C(1)<<55)+121);
    assert(tm_rtl_prepare_dot(0,NULL,NULL,0,packets,1,&expected)==1&&packets[0]==(UINT64_C(1)<<55));
    packets[0]=77;expected.result=77;expected.error=true;
    assert(tm_rtl_prepare_dot(2,w,a,6,packets,3,&expected)==-80);
    assert(tm_rtl_prepare_dot(1,w,a,6,packets,1,&expected)==-81);
    a[5]=128;assert(tm_rtl_prepare_dot(1,w,a,6,packets,2,&expected)==-80);a[5]=1;
    w[5]=2;assert(tm_rtl_prepare_dot(1,w,a,6,packets,2,&expected)==-80);w[5]=1;
    assert(packets[0]==77&&expected.result==77&&expected.error);
    int32_t acts[]={-128,-1,0,1,127};
    assert(tm_rtl_packet(1023,acts,31,true,encoded)==0);
    assert((encoded[0]&1023)==1023&&encoded[0]>>50==63);
    for(unsigned i=0;i<5;i++)assert(((encoded[0]>>(10+8*i))&255)==((uint32_t)acts[i]&255));
    uint64_t old=encoded[0];assert(tm_rtl_packet(1024,acts,31,true,encoded)==-80&&encoded[0]==old);
    assert(tm_rtl_packet(0,acts,32,true,encoded)==-80&&encoded[0]==old);
    expected=(TMRtlExpected){-1,true};
    assert(tm_rtl_expected_words(&expected,1,encoded,1)==0&&encoded[0]==UINT64_C(0x1ffffffff));
    uint8_t text[35];memset(text,0xa5,sizeof text);
    assert(tm_rtl_write_mem(encoded,1,text,16)==-81&&text[0]==0xa5);
    assert(tm_rtl_write_mem(encoded,1,text,17)==17&&!memcmp(text,"00000001ffffffff\n",17)&&text[17]==0xa5);
    assert(tm_rtl_expected_words(&expected,1,encoded,0)==-81);
    expected.result=INT64_MAX;assert(tm_rtl_expected_words(&expected,1,encoded,1)==-80);
}
static const char *valid="warning: diagnostic\nDOT_RESULT index=0 result=-128 error=0 cycles=10\nDOT_RESULT index=1 result=0 error=1 cycles=20\nDOT_SUMMARY cycles=24 groups=3 input_stalls=1 output_stalls=6 source_bubbles=1 resets=0\n";
static TMRtlExpected expected[]={{-128,false},{0,true}};
static void parsing(void) {
    TMRtlObserved observed[2];TMRtlSummary summary={.cycles=77};
    assert(tm_rtl_parse_output((uint8_t*)valid,strlen(valid),expected,2,observed,2,&summary)==0);
    assert(summary.cycles==24&&summary.groups==3&&summary.input_stalls==1&&summary.output_stalls==6&&summary.result_count==2);
    assert(observed[0].result==-128&&!observed[0].error&&observed[1].error&&observed[1].cycles==20);
    assert(tm_rtl_parse_output((uint8_t*)valid,strlen(valid),expected,2,observed,1,&summary)==-81);
    assert(tm_rtl_parse_output((uint8_t*)valid,strlen(valid),expected,0,observed,2,&summary)==-80);
    char text[1024];strcpy(text,valid);strstr(text,"index=1")[6]='0';
    summary.cycles=77;assert(tm_rtl_parse_output((uint8_t*)text,strlen(text),expected,2,observed,2,&summary)==-84&&summary.cycles==77);
    strcpy(text,valid);strstr(text,"result=-128")[10]='9';
    assert(tm_rtl_parse_output((uint8_t*)text,strlen(text),expected,2,observed,2,&summary)==-84);
    strcpy(text,valid);strstr(text,"error=1")[6]='2';
    assert(tm_rtl_parse_output((uint8_t*)text,strlen(text),expected,2,observed,2,&summary)==-83);
    strcpy(text,valid);strstr(text,"cycles=20")[7]='0';
    assert(tm_rtl_parse_output((uint8_t*)text,strlen(text),expected,2,observed,2,&summary)==-83);
    strcpy(text,valid);strstr(text,"groups=3")[7]='0';
    assert(tm_rtl_parse_output((uint8_t*)text,strlen(text),expected,2,observed,2,&summary)==-83);
    strcpy(text,valid);strcat(text,"DOT_SUMMARY cycles=24 groups=3 input_stalls=1 output_stalls=6 source_bubbles=1 resets=0\n");
    assert(tm_rtl_parse_output((uint8_t*)text,strlen(text),expected,2,observed,2,&summary)==-83);
    strcpy(text,valid);strcat(text,"DOT_UNKNOWN x=1\n");
    assert(tm_rtl_parse_output((uint8_t*)text,strlen(text),expected,2,observed,2,&summary)==-83);
    /* Every truncation within scored records must fail. */
    size_t begin=(size_t)(strstr(valid,"DOT_RESULT")-valid);
    size_t end=strlen(valid)-1;
    for(size_t n=begin;n<end;n++)assert(tm_rtl_parse_output((uint8_t*)valid,n,expected,2,observed,2,&summary)<0);
    assert(tm_rtl_parse_output((uint8_t*)valid,end,expected,2,observed,2,&summary)==0);
}
static void simulation(void) {
    int32_t weights[]={-1,0,1},activations[]={-128,1,127};
    TMRtlWitness witness={.result=77};
    for(int codec=0;codec<2;codec++){
        assert(tm_rtl_run_dot(weights,activations,3,codec,27,&witness)==0);
        assert(witness.result==255&&!witness.error&&witness.groups==1&&witness.output_stalls>=3);
        assert(witness.weight_count==3&&witness.encoded_weight_bits==(uint64_t)(codec?8:10));
        assert(tm_rtl_run_dot(NULL,NULL,0,codec,0,&witness)==0&&witness.result==0&&!witness.error);
    }
    witness.result=77;
    assert(tm_rtl_run_dot(weights,activations,3,1,-1,&witness)==-80&&witness.result==77);
    assert(tm_rtl_run_dot(weights,activations,3,1,INT64_C(4294967296),&witness)==-80&&witness.result==77);
    uint64_t packet;TMRtlExpected wanted;TMRtlObserved observed;TMRtlSummary summary={.cycles=77};
    assert(tm_rtl_prepare_dot(1,weights,activations,3,&packet,1,&wanted)==1);
    wanted.result=256;
    /* Wrong independent scoreboard makes the real simulator exit nonzero. */
    assert(tm_rtl_run_frames(&packet,1,&wanted,1,1,27,32,&observed,1,&summary)==-85&&summary.cycles==77);
}
int main(void){preparation();parsing();simulation();puts("native RTL packet/file and observed-scoreboard parsing checks passed");return 0;}
