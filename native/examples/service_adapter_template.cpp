#include "harmony_ops/pipeline.hpp"

#include <stdexcept>

// Copy these adapters into an existing Native user-space system service and
// replace each throw only after the service's real focus, /proc access,
// scheduling, and output contracts have been verified on the target device.
namespace service_adapter_template {

class HostPermissionGate final : public harmony_ops::PermissionGate {
public:
    void Check() override {
        throw std::logic_error("TODO: verify service identity and access to required observations");
    }
};

class HostWindowScheduler final : public harmony_ops::WindowScheduler {
public:
    std::optional<harmony_ops::WindowPlan> Next() override {
        throw std::logic_error("TODO: schedule baseline, action and post-action windows");
    }
};

class HostApplicationIdentity final : public harmony_ops::ApplicationIdentityProvider {
public:
    harmony_ops::AppIdentity Identify(const harmony_ops::WindowPlan&) override {
        throw std::logic_error("TODO: use a verified independent foreground/focus evidence source");
    }
};

class HostReferencedFeatures final : public harmony_ops::ReferencedFeatureProvider {
public:
    harmony_ops::FeatureWindow Build(const harmony_ops::WindowPlan&,
                                     const harmony_ops::AppIdentity&) override {
        throw std::logic_error("TODO: collect real Referenced windows and build matching schema features");
    }
};

class HostResultSink final : public harmony_ops::ResultSink {
public:
    void Emit(const harmony_ops::WindowResult&) override {
        throw std::logic_error("TODO: write predictions using the service's approved output channel");
    }
};

}  // namespace service_adapter_template
