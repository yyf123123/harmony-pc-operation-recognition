#include "harmony_ops/pipeline.hpp"

#include <stdexcept>
#include <string>
#include <vector>

using namespace harmony_ops;

namespace {

struct Permission : PermissionGate {
    int checks = 0;
    void Check() override { ++checks; }
};

struct Schedule : WindowScheduler {
    int next = 0;
    std::optional<WindowPlan> Next() override {
        if (++next > 3) return std::nullopt;
        return WindowPlan{std::to_string(next), static_cast<std::uint64_t>(next),
                          static_cast<std::uint64_t>(next + 1)};
    }
};

struct Identity : ApplicationIdentityProvider {
    AppIdentity Identify(const WindowPlan& plan) override {
        return {plan.window_id == "1" ? "bilibili" :
                plan.window_id == "2" ? "huawei_browser" : "wps",
                "service-focus-adapter"};
    }
};

struct Features : ReferencedFeatureProvider {
    FeatureWindow Build(const WindowPlan& plan, const AppIdentity& app) override {
        std::vector<double> vector(kVectorDimension, 0.0);
        vector[0] = 1.0;
        return {plan.window_id, SpecFor(app.app_id).feature_schema, vector};
    }
};

struct Sink : ResultSink {
    std::vector<std::string> apps;
    void Emit(const WindowResult& result) override {
        if (result.prediction.operation_label.empty())
            throw std::runtime_error("empty prediction");
        apps.push_back(result.app.app_id);
    }
};

}  // namespace

int main(int argc, char** argv) {
    if (argc != 2) return 2;
    NativePipeline pipeline(argv[1]);
    Permission permission;
    Schedule schedule;
    Identity identity;
    Features features;
    Sink sink;
    if (!pipeline.RunOne(permission, schedule, identity, features, sink) ||
        !pipeline.RunOne(permission, schedule, identity, features, sink) ||
        !pipeline.RunOne(permission, schedule, identity, features, sink) ||
        pipeline.RunOne(permission, schedule, identity, features, sink) ||
        sink.apps != std::vector<std::string>{"bilibili", "huawei_browser", "wps"} ||
        permission.checks != 4)
        return 1;
    return 0;
}
