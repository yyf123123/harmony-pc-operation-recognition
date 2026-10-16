#ifndef HARMONY_OPS_PLATFORM_BRIDGE_H
#define HARMONY_OPS_PLATFORM_BRIDGE_H

#include "harmony_ops/service.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Thin host glue implemented in native/examples/platform_bridge.cpp.
 * Compile that source once into the existing host service, alongside linking
 * the supplied static libraries. These two functions are not part of the core
 * static archive. Edit only its closed-platform focus call for the target API;
 * generic collection, scheduling, features and history remain in the core.
 */

/* Fill verified app/window/root PID/current required child coverage. Return
 * 0 for success, -1 when no platform provider was compiled, any other value for
 * an actual provider failure. The supplied implementation returns -1 until the
 * target focus API is connected. All nonzero results fail live host capture;
 * the standalone Linux entry may use its supported fallback only for -1.
 * Context and output belong to the caller. Never throw across this boundary.
 */
int ReadOperationFocus(void *host_context, hop_focus *out);

/* Create and start a runtime using the supplied host callbacks. A NULL
 * read_foreground selects ReadOperationFocus from the same bridge source.
 * NULL publish_result uses config.output_jsonl; NULL notify_stage is allowed.
 * The returned handle is controlled by hop_run/request_stop/stop/destroy.
 * On failure *out remains NULL. Callback ownership/lifetime follows service.h.
 */
hop_status StartOperationRecognition(
    const hop_config *config, void *host_context,
    int (*read_foreground)(void *, hop_focus *),
    int (*publish_result)(void *, const char *, size_t),
    int (*notify_stage)(void *, const char *, size_t, uint64_t),
    hop_handle **out, hop_error *error);

#ifdef __cplusplus
}
#endif
#endif
