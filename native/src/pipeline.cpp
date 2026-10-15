#include "harmony_ops/pipeline.hpp"

#include <stdexcept>

namespace harmony_ops {

bool NativePipeline::RunOne(PermissionGate& permission, WindowScheduler& schedule,
                            ApplicationIdentityProvider& identity,
                            ReferencedFeatureProvider& features, ResultSink& sink) {
    permission.Check();
    const auto plan = schedule.Next();
    if (!plan) return false;
    if (plan->window_id.empty() || plan->monotonic_end_ns <= plan->monotonic_start_ns)
        throw std::invalid_argument("invalid window plan or monotonic interval");
    const AppIdentity app = identity.Identify(*plan);
    if (app.app_id.empty() || app.evidence_source.empty())
        throw std::invalid_argument("application identity and evidence source are required");
    const ModelSpec& spec = SpecFor(app.app_id);
    const FeatureWindow window = features.Build(*plan, app);
    if (window.window_id != plan->window_id || window.feature_schema != spec.feature_schema)
        throw std::invalid_argument("feature window ID or schema mismatch");
    auto found = models_.find(app.app_id);
    if (found == models_.end()) {
        found = models_.emplace(app.app_id,
                                CentroidModel::Load(models_root_, app.app_id, spec.feature_schema)).first;
    }
    const Prediction prediction = found->second.Predict(window.vector);
    sink.Emit(WindowResult{*plan, app, prediction});
    return true;
}

}  // namespace harmony_ops
