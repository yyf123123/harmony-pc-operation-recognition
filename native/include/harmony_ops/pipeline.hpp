#pragma once

#include <cstdint>
#include <map>
#include <optional>
#include <string>
#include <utility>
#include <vector>

#include "harmony_ops/recognition.hpp"

namespace harmony_ops {

struct WindowPlan {
    std::string window_id;
    std::uint64_t monotonic_start_ns = 0;
    std::uint64_t monotonic_end_ns = 0;
};

struct AppIdentity {
    std::string app_id;
    std::string evidence_source;  // supplied by the independent service adapter
};

struct FeatureWindow {
    std::string window_id;
    std::string feature_schema;
    std::vector<double> vector;
};

struct WindowResult {
    WindowPlan plan;
    AppIdentity app;
    Prediction prediction;
};

// These interfaces intentionally have no HarmonyOS or kernel dependency.
// The host system service must provide implementations and verify permissions.
class PermissionGate {
public:
    virtual ~PermissionGate() = default;
    virtual void Check() = 0;
};
class WindowScheduler {
public:
    virtual ~WindowScheduler() = default;
    virtual std::optional<WindowPlan> Next() = 0;
};
class ApplicationIdentityProvider {
public:
    virtual ~ApplicationIdentityProvider() = default;
    virtual AppIdentity Identify(const WindowPlan& plan) = 0;
};
class ReferencedFeatureProvider {
public:
    virtual ~ReferencedFeatureProvider() = default;
    virtual FeatureWindow Build(const WindowPlan& plan, const AppIdentity& app) = 0;
};
class ResultSink {
public:
    virtual ~ResultSink() = default;
    virtual void Emit(const WindowResult& result) = 0;
};

class NativePipeline {
public:
    explicit NativePipeline(std::filesystem::path models_root)
        : models_root_(std::move(models_root)) {}
    // Processes one independent window. No smoothing or switch hysteresis.
    // Returns false when the scheduler has no next window.
    bool RunOne(PermissionGate& permission, WindowScheduler& schedule,
                ApplicationIdentityProvider& identity,
                ReferencedFeatureProvider& features, ResultSink& sink);

private:
    std::filesystem::path models_root_;
    std::map<std::string, CentroidModel> models_;
};

}  // namespace harmony_ops
