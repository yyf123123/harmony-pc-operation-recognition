#include "harmony_ops/service.hpp"

// A compiling service entry sharing the exact opsctl collection/runtime core.
// The host supplies its closed focus API through callbacks.focus and optionally
// callbacks.publish. Procfs, scheduling, model verification, raw features,
// failure handling, cancellation and history already live in the library.
namespace service_adapter {

harmony_ops::Service Connect(const hop_config& device_config,
                            const hop_callbacks& platform_callbacks) {
    return harmony_ops::Service(device_config,platform_callbacks);
}

// Host worker usage:
// auto service=service_adapter::Connect(config,callbacks);
// service.Start();
// service.Run("unlabeled-window-0001","recognized-event-0001");
// service.Stop();
// A service control thread can call RequestStop() while Run() is waiting.

} // namespace service_adapter
