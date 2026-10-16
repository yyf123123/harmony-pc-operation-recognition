#ifndef HARMONY_OPS_COLLECTOR_H
#define HARMONY_OPS_COLLECTOR_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* No function terminates the process or writes to stdout/stderr. All allocations
 * in a successful snapshot belong to the library; release with snapshot_free.
 * Output snapshots must be zero initialized. A failed call leaves them empty.
 * One snapshot covers one explicitly selected process. The caller must discard
 * a multi-process window if any process fails; never aggregate partial results.
 */
typedef enum hc_status {
    HC_OK = 0,
    HC_INVALID_ARGUMENT,
    HC_UNSUPPORTED,
    HC_PERMISSION_DENIED,
    HC_IO_ERROR,
    HC_PARSE_ERROR,
    HC_MISSING_REFERENCED,
    HC_EMPTY_SNAPSHOT,
    HC_PROCESS_CHANGED,
    HC_NO_MEMORY
} hc_status;

typedef enum hc_segment_mode {
    HC_SEGMENT_WPS = 0,
    HC_SEGMENT_CROSS_APP = 1
} hc_segment_mode;

typedef struct hc_error {
    hc_status code;
    int system_errno;
    int32_t pid;
    char message[256];
} hc_error;

typedef struct hc_process_identity {
    int32_t pid;
    uint64_t start_ticks;
} hc_process_identity;

typedef struct hc_vma {
    uint64_t start;
    uint64_t end;
    uint64_t offset;
    uint64_t inode;
    uint64_t referenced_bytes;
    /* ceil(referenced_bytes / page_size), as frozen WPS compact collection.
     * Cross-app features use referenced_bytes / page_size as a double. */
    uint64_t referenced_pages;
    char permissions[8];
    char device[32];
    char pathname[4096];
    char segment[32];
} hc_vma;

typedef struct hc_snapshot {
    hc_process_identity identity;
    uint64_t page_size;
    /* CLOCK_MONOTONIC timestamps for live collection; zero for text fixtures. */
    uint64_t started_ns;
    uint64_t finished_ns;
    hc_vma *vmas;
    size_t count;
} hc_snapshot;

const char *hc_status_name(hc_status status);
/* Pure parser available on all platforms. It requires complete newline-terminated
 * smaps text with exactly one valid Referenced field per VMA. Explicit segment
 * fields in cross-app fixtures retain collector/smaps_collector.py semantics. */
hc_status hc_parse_smaps(const char *text, size_t length, uint64_t page_size,
                         hc_segment_mode mode, hc_snapshot *out, hc_error *error);
/* Linux/OpenHarmony procfs only. Identity includes stat field 22 starttime;
 * zombies/dead processes and changes before/after I/O fail explicitly. */
hc_status hc_read_identity(int32_t pid, hc_process_identity *out, hc_error *error);
hc_status hc_collect(const hc_process_identity *expected, hc_segment_mode mode,
                     hc_snapshot *out, hc_error *error);
/* Side effect: writes exactly "1\n"; use only for an authorized sampling target.
 * An error may follow a completed write if the process changed during the call.
 * The caller must invalidate the complete window on every non-OK result. */
hc_status hc_clear_refs(const hc_process_identity *expected, hc_error *error);
void hc_snapshot_free(hc_snapshot *snapshot);

#ifdef __cplusplus
}
#endif
#endif
