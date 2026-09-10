/* Native experiment/report smoke + error paths. Actual loopback TCP is required;
 * Python reference and real RTL differential checks are separate tests. */
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "api.h"
static uint8_t report[65536], html[262144];
int main(void) {
    int64_t n=tm_exp_edge(false,27,report,sizeof report-1);
    assert(n>0);report[n]=0;
    assert(strstr((char*)report,"\"fixture_count\":6"));
    assert(strstr((char*)report,"\"runtime\":\"native-t27\""));
    assert(strstr((char*)report,"\"accumulators\":[480,-480,0]"));
    int64_t rendered=tm_report_edge_html(report,(size_t)n,html,sizeof html-1);
    assert(rendered>0);html[rendered]=0;
    assert(strstr((char*)html,"step_up")&&strstr((char*)html,"&quot;schema&quot;"));
    assert(tm_report_edge_html(report,(size_t)n,html,1)<0);
    assert(tm_exp_edge(false,27,report,1)<0);
    assert(tm_exp_edge(true,-1,report,sizeof report)<0);
    assert(tm_exp_edge(true,4294967294LL,report,sizeof report)<0);
    uint8_t fixture[8192];FILE *file=fopen("examples/conformance.json","rb");assert(file);
    size_t fixture_size=fread(fixture,1,sizeof fixture,file);assert(fclose(file)==0);
    n=tm_exp_conformance(fixture,fixture_size,false,27,report,sizeof report-1);
    assert(n>0);report[n]=0;
    assert(strstr((char*)report,"\"positive_checks\":46"));assert(strstr((char*)report,"\"corrupt_rejections\":6"));
    assert(tm_exp_conformance(fixture,fixture_size,true,-1,report,sizeof report)<0);
    assert(tm_exp_conformance((uint8_t*)"{}",2,false,27,report,sizeof report)==TM_EXP_ERR_CHECK);
    assert(tm_exp_conformance((uint8_t*)"{}",2,true,27,report,sizeof report)==TM_EXP_ERR_CHECK);
    assert(tm_exp_conformance(fixture,fixture_size,false,27,report,1)<0);
    n=tm_exp_benchmark(127,3,27,report,sizeof report-1);assert(n>0);report[n]=0;
    assert(strstr((char*)report,"\"weights_per_dataset\":127"));
    rendered=tm_report_benchmark_html(report,(size_t)n,html,sizeof html-1);
    assert(rendered>0);html[rendered]=0;
    assert(strstr((char*)html,"WebAssembly.instantiate")&&strstr((char*)html,"!== 1n"));
    assert(!strstr((char*)html,"digits.reduce"));
    assert(tm_report_benchmark_html(report,(size_t)n,html,1)<0);
    assert(tm_report_benchmark_html((uint8_t*)"{}",2,html,sizeof html)<0);
    for(size_t count=1;count<24;++count)assert(tm_exp_benchmark(count,2,27,report,sizeof report)>0);
    assert(tm_exp_benchmark(0,1,27,report,sizeof report)<0);
    assert(tm_exp_benchmark(1,0,27,report,sizeof report)<0);
    assert(tm_exp_benchmark(4194305,1,27,report,sizeof report)<0);
    assert(tm_exp_benchmark(1,1000001,27,report,sizeof report)<0);
    assert(tm_exp_benchmark(1,1,27,report,1)<0);
    uint64_t times[]={100,0,2,1};assert(tm_exp_median_ns(times,4)==2);
    uint64_t limits[]={UINT64_MAX-1,UINT64_MAX};assert(tm_exp_median_ns(limits,2)==UINT64_MAX-1);
    assert(tm_exp_signal_model(1,report,sizeof report)>0);
    assert(tm_exp_signal_model(4,report,sizeof report)<0);
    n=tm_exp_fixtures_json(report,sizeof report-1);assert(n>0);report[n]=0;
    assert(strstr((char*)report,"noisy_alternating"));
    puts("PASS native experiments/report: actual TCP, reference checks, corruption rejection, bounded timing and HTML");
}
