#include "harmony_ops/service.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define CHECK(condition) do { if (!(condition)) { fprintf(stderr,"C ABI check failed at line %d\n",__LINE__); return 1; } } while (0)
typedef struct test_host {
    int action, successes, errors, fail_focus, fail_output, fail_clear, missing_referenced;
} test_host;
static int focus(void *context,hop_focus *out) {
    test_host *host=(test_host*)context;
    if (host->fail_focus) return 1;
    strcpy(out->app_id,"wps"); strcpy(out->window_id,"synthetic-window");
    strcpy(out->evidence_source,"synthetic-c-abi-focus");
    out->root_pid=123; out->covered_pids[0]=123; out->pid_count=1;
    return 0;
}
static int stage(void *context,const char *phase,size_t index,uint64_t ns) {
    test_host *host=(test_host*)context;
    (void)index; (void)ns;
    host->action=strcmp(phase,"baseline")!=0;
    return 0;
}
static int identity(void *context,int32_t pid,uint64_t *ticks) {
    (void)context; (void)pid; *ticks=456; return 0;
}
static int clear_refs(void *context,int32_t pid,uint64_t ticks) {
    (void)pid; (void)ticks;
    return ((test_host*)context)->fail_clear;
}
static int read_smaps(void *context,int32_t pid,uint64_t ticks,char *buffer,size_t capacity,size_t *length,uint64_t *page_size) {
    test_host *host=(test_host*)context;
    const char *raw=host->missing_referenced?"1000-2000 rw-p 0 00:00 0 [heap]\nSize: 4 kB\n":
        host->action?"1000-2000 rw-p 0 00:00 0 [heap]\nReferenced: 4 kB\n":
        "1000-2000 rw-p 0 00:00 0 [heap]\nReferenced: 0 kB\n";
    (void)pid; (void)ticks;
    *length=strlen(raw); *page_size=4096;
    if (!buffer && !capacity) return 0;
    if (capacity<*length) return 1;
    memcpy(buffer,raw,*length);
    return 0;
}
static int publish(void *context,const char *json,size_t length) {
    test_host *host=(test_host*)context;
    if (host->fail_output) return 1;
    if (!json || !length) return 1;
    if (strstr(json,"\"status\":\"success\"")) ++host->successes;
    if (strstr(json,"\"status\":\"error\"")) ++host->errors;
    return 0;
}
int main(int argc,char **argv) {
    hop_config config;
    hop_callbacks callbacks;
    hop_handle *handle=NULL;
    hop_error error;
    test_host host={0};
    size_t required=0;
    char tiny[1], *json, manifest[4096];
    CHECK(argc==3);
    hop_default_config(&config); hop_default_callbacks(&callbacks);
    CHECK(config.abi_version==HOP_ABI_VERSION && config.struct_size==sizeof(config));
    config.models_root=argv[1]; config.session_id="synthetic-c-abi-session";
    config.baseline_ms=10; config.action_ms=10; config.post_ms=10; config.focus_poll_ms=1;
    config.max_sample_ms=100;
    callbacks.context=&host; callbacks.focus=focus; callbacks.stage=stage;
    callbacks.identity=identity; callbacks.clear_refs=clear_refs;
    callbacks.read_smaps=read_smaps; callbacks.publish=publish;
    config.abi_version=999;
    CHECK(hop_create(&config,&callbacks,&handle,&error)==HOP_INVALID_ARGUMENT && !handle);
    config.abi_version=HOP_ABI_VERSION;
    CHECK(hop_create(&config,&callbacks,&handle,&error)==HOP_OK && handle);
    CHECK(hop_run(handle,"w","e",&error)==HOP_NOT_STARTED);
    CHECK(hop_start(handle,&error)==HOP_OK);
    CHECK(snprintf(manifest,sizeof(manifest),"%s/manifest.csv",argv[2])>0);
    CHECK(hop_replay(handle,manifest,NULL,0,&required,&error)==HOP_OK && required>2);
    json=(char*)malloc(required);
    CHECK(json && hop_replay(handle,manifest,json,required,&required,&error)==HOP_OK);
    CHECK(strstr(json,"offline_replay") && host.successes==0 && host.errors==0);
    free(json);
    CHECK(hop_history_json(handle,NULL,0,&required,&error)==HOP_OK && required==3);
    CHECK(hop_run(handle,"window1","event1",&error)==HOP_OK && host.successes==1);
    CHECK(hop_run(handle,"window1","event1",&error)==HOP_DUPLICATE_EVENT && host.successes==1);
    CHECK(hop_history_json(handle,NULL,0,&required,&error)==HOP_OK && required>2);
    CHECK(hop_history_json(handle,tiny,sizeof(tiny),&required,&error)==HOP_INVALID_ARGUMENT);
    json=(char*)malloc(required);
    CHECK(json && hop_history_json(handle,json,required,&required,&error)==HOP_OK);
    CHECK(strstr(json,"event1") && strstr(json,"wps:123:456"));
    free(json);
    CHECK(hop_health_json(handle,NULL,0,&required,&error)==HOP_OK && required>1);
    json=(char*)malloc(required);
    CHECK(json && hop_health_json(handle,json,required,&required,&error)==HOP_OK);
    CHECK(strstr(json,"\"completed_windows\":1"));
    free(json);
    host.fail_focus=1;
    CHECK(hop_run(handle,"bad-window","bad-focus",&error)==HOP_FOCUS_CHANGED && host.successes==1 && host.errors==1);
    host.fail_focus=0; host.fail_clear=1;
    CHECK(hop_run(handle,"bad-window","bad-clear",&error)==HOP_COLLECTION_FAILED && host.successes==1);
    host.fail_clear=0; host.missing_referenced=1;
    CHECK(hop_run(handle,"bad-window","bad-field",&error)==HOP_COLLECTION_FAILED && host.successes==1);
    host.missing_referenced=0; host.fail_output=1;
    CHECK(hop_run(handle,"bad-window","bad-output",&error)==HOP_OUTPUT_FAILED && host.successes==1);
    CHECK(hop_stop(handle,&error)==HOP_OK);
    CHECK(hop_start(handle,&error)==HOP_OK);
    host.fail_output=0;
    CHECK(hop_run(handle,"window2","event1",&error)==HOP_OK && host.successes==2);
    CHECK(hop_request_stop(handle,&error)==HOP_OK);
    CHECK(hop_run(handle,"window3","cancelled",&error)==HOP_CANCELLED && host.successes==2);
    CHECK(hop_stop(handle,&error)==HOP_OK);
    hop_destroy(handle);
    CHECK(hop_health_json(NULL,NULL,0,&required,&error)==HOP_INVALID_ARGUMENT);
    printf("C ABI tests passed: pure C host, callbacks, ownership, failures, cancellation\n");
    return 0;
}
