#ifndef HARMONY_OPS_SERVICE_H
#define HARMONY_OPS_SERVICE_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define HOP_ABI_VERSION 1u
#define HOP_MAX_PIDS 256u
typedef struct hop_handle hop_handle;
typedef enum hop_status {
    HOP_OK=0, HOP_INVALID_ARGUMENT, HOP_NOT_STARTED, HOP_BUSY, HOP_CANCELLED,
    HOP_FOCUS_CHANGED, HOP_PROCESS_CHANGED, HOP_COLLECTION_FAILED,
    HOP_SAMPLING_TIMEOUT, HOP_MODEL_ERROR, HOP_OUTPUT_FAILED,
    HOP_DUPLICATE_EVENT, HOP_INTERNAL_ERROR
} hop_status;
typedef struct hop_error { hop_status code; char message[256]; } hop_error;
typedef struct hop_config {
    uint32_t abi_version;
    size_t struct_size;
    const char *models_root, *app_id, *session_id, *output_jsonl;
    uint32_t baseline_ms, action_ms, post_ms, max_sample_ms, focus_poll_ms;
    size_t baseline_count, history_limit, dedup_limit;
} hop_config;
typedef struct hop_focus {
    char app_id[64], window_id[128], evidence_source[128];
    int32_t root_pid;
    int32_t covered_pids[HOP_MAX_PIDS];
    size_t pid_count;
} hop_focus;
typedef struct hop_callbacks {
    uint32_t abi_version;
    size_t struct_size;
    void *context;
    /* All callbacks return 0 on success; nonzero invalidates the full window.
     * Borrowed strings/buffers are valid only during each callback. Never free
     * library memory. No exception may escape a callback. */
    int (*focus)(void *context, hop_focus *out);
    int (*stage)(void *context, const char *phase, size_t index, uint64_t monotonic_ns);
    int (*publish)(void *context, const char *json, size_t length);
    /* Optional overrides. NULL uses strict /proc support. read_smaps is called
     * first with NULL/0 to obtain size, then with library-owned storage. It must
     * return complete newline-terminated text and report the actual page size.
     * identity must provide process start ticks, not a wall-clock timestamp. */
    int (*identity)(void *context, int32_t pid, uint64_t *start_ticks);
    int (*clear_refs)(void *context, int32_t pid, uint64_t start_ticks);
    int (*read_smaps)(void *context, int32_t pid, uint64_t start_ticks,
                      char *buffer, size_t capacity, size_t *length, uint64_t *page_size);
} hop_callbacks;

/* Populate defaults before editing a config/callback struct. All pointer fields
 * are copied by create; context itself remains owned by the host and must live
 * until destroy. Functions never throw or expose STL across this C boundary. */
void hop_default_config(hop_config *out);
void hop_default_callbacks(hop_callbacks *out);
hop_status hop_create(const hop_config*, const hop_callbacks*, hop_handle **out, hop_error*);
hop_status hop_start(hop_handle*, hop_error*);
/* Blocking controlled trigger; no operation name/ground truth is accepted.
 * Run on the host worker. Stop from another thread cancels and joins its work. */
hop_status hop_run(hop_handle*, const char *window_id, const char *event_id, hop_error*);
hop_status hop_request_stop(hop_handle*, hop_error*);
hop_status hop_stop(hop_handle*, hop_error*);
/* Query required bytes including NUL using buffer=NULL, capacity=0. The caller
 * allocates/frees its own buffer. Too small returns INVALID_ARGUMENT and reports
 * required size. These snapshots do not invoke platform callbacks. */
hop_status hop_health_json(hop_handle*, char *buffer, size_t capacity, size_t *required, hop_error*);
hop_status hop_history_json(hop_handle*, char *buffer, size_t capacity, size_t *required, hop_error*);
/* Host-internal offline raw replay before permissions/live integration. The
 * manifest uses phase,index,pid,page_size,smaps CSV. This pure query never calls
 * focus/clear/publish, never updates recognition history, and never proves live
 * target compatibility. Buffer query/copy has the same ownership as above. */
hop_status hop_replay(hop_handle*, const char *manifest_csv, char *buffer,
                      size_t capacity, size_t *required, hop_error*);
/* Caller must stop/join any concurrent host calls before destroy. Stop must not
 * be called inside a callback; use request_stop there. */
void hop_destroy(hop_handle*);
const char *hop_status_name(hop_status);

#ifdef __cplusplus
}
#endif
#endif
