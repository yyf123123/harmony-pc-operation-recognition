#include "harmony_ops/service.h"

// Replace this single closed-platform call in the inner network. It must fill
// app/window/root PID/current child coverage from an actual focus authority.
// Returning failure keeps standalone diagnostics/replay usable and live capture
// explicitly unavailable until the real system API has been connected.
// -1 means no compiled platform provider; 0 success; other values failure.
extern "C" int ReadOperationFocus(void*,hop_focus*) { return -1; }

// The only host-specific wiring point. Existing Native services pass their
// actual foreground/window/root-process adapter here. Missing/failed focus
// produces focus_changed. publish may be NULL to use config.output_jsonl.
// A label-free stage callback can notify a controlled automation runner.
extern "C" hop_status StartOperationRecognition(
    const hop_config* config,void* host_context,
    int (*read_foreground)(void*,hop_focus*),
    int (*publish_result)(void*,const char*,size_t),
    int (*notify_stage)(void*,const char*,size_t,uint64_t),
    hop_handle** out,hop_error* error) {
    hop_callbacks callbacks;
    hop_default_callbacks(&callbacks);
    callbacks.context=host_context;
    callbacks.focus=read_foreground?read_foreground:ReadOperationFocus;
    callbacks.publish=publish_result;
    callbacks.stage=notify_stage;
    const auto created=hop_create(config,&callbacks,out,error);
    if (created!=HOP_OK) return created;
    const auto started=hop_start(*out,error);
    if (started!=HOP_OK) { hop_destroy(*out); *out=nullptr; }
    return started;
}
