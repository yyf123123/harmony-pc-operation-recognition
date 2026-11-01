#ifndef HARMONY_OPS_EMBEDDED_H
#define HARMONY_OPS_EMBEDDED_H
#include "harmony_ops/service.h"
#ifdef __cplusplus
extern "C" {
#endif

typedef struct hop_embedded hop_embedded;
/* Three-call integration for an EXISTING native service. No OS service is
 * registered by this library. Start checks the frozen model and synthetic raw
 * golden automatically, creates a unique run directory below output_root, and
 * starts its own idle worker. It performs no live collection until trigger.
 * package_root contains models/ and fixtures/raw-window/. app_id is one frozen
 * app: wps, bilibili, huawei_browser. No Linux candidate is selected here.
 * ConfigureOperationCallbacks in platform_bridge.cpp supplies real focus.
 * All callbacks and their contexts must remain alive until stop returns.
 * Initialize *out=NULL. Repeated start on a live handle returns HOP_BUSY.
 * Start success means offline_ready, NOT live permissions/device validation.
 */
hop_status hop_embedded_start(const char *package_root, const char *output_root,
                             const char *app_id, hop_embedded **out, hop_error *error);
/* Nonblocking, label-free controlled trigger. HOP_OK means ACCEPTED, not a
 * prediction. One outstanding window; HOP_BUSY rejects a second (no queue).
 * Observe stage.jsonl for action and status.txt/results.jsonl for completion.
 */
hop_status hop_embedded_trigger(hop_embedded *service, hop_error *error);
/* Cancel active work, join the owned worker and destroy; sets *service=NULL.
 * Call on host shutdown/control thread, never inside a platform callback.
 * Host must prevent new API calls once shutdown starts. NULL is idempotent.
 */
hop_status hop_embedded_stop(hop_embedded **service, hop_error *error);

#ifdef __cplusplus
}
#endif
#endif
