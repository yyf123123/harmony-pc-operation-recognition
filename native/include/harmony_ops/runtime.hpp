#pragma once

#include "harmony_ops/collector.h"
#include "harmony_ops/feature.hpp"

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <deque>
#include <filesystem>
#include <memory>
#include <mutex>
#include <set>
#include <stdexcept>
#include <string>
#include <vector>

namespace harmony_ops {

// WPS normalizes pathname whitespace/repeated slashes and uses collector WPS
// segments + ceil page counts. Cross-app v2 preserves its distinct Python rules.
FeatureMap SemanticFeatures(const hc_snapshot&, const std::string& schema);
FeatureMap BaselineMedian(const std::vector<FeatureMap>&);
FeatureMap ExcessPages(const std::vector<FeatureMap>&, const FeatureMap& action,
                       const FeatureMap& post);
std::vector<double> BuildWindowVector(const std::string& schema,
    const std::vector<FeatureMap>&, const FeatureMap& action, const FeatureMap& post);
std::string JsonString(const std::string&);
std::uint64_t MonotonicNowNs();

enum class RuntimeCode { Ok=0, InvalidArgument, NotStarted, Busy, Cancelled,
    FocusChanged, ProcessChanged, CollectionFailed, SamplingTimeout, ModelError,
    OutputFailed, DuplicateEvent, InternalError };
const char* RuntimeCodeName(RuntimeCode) noexcept;
class RuntimeError : public std::runtime_error {
public:
    RuntimeError(RuntimeCode code, const std::string& message)
        : std::runtime_error(message), code_(code) {}
    RuntimeCode code() const noexcept { return code_; }
private: RuntimeCode code_;
};

struct FocusEvidence {
    std::string app_id, window_id, evidence_source;
    std::int32_t root_pid = 0;
    std::vector<std::int32_t> covered_pids;
};
struct RuntimeConfig {
    std::filesystem::path models_root;
    std::string app_id = "wps", session_id;
    std::filesystem::path output_jsonl;
    std::uint32_t baseline_ms=5000, action_ms=15000, post_ms=5000;
    std::size_t baseline_count=2, history_limit=64, dedup_limit=1024;
    std::uint32_t max_sample_ms=1000, focus_poll_ms=100;
};
struct RecognizedEvent {
    std::string event_id, window_id, session_id, app_id, app_instance_id;
    std::string focus_window_id, evidence_source, coverage_id;
    std::uint64_t start_ns=0, end_ns=0;
    Prediction prediction;
    std::vector<hc_process_identity> processes;
    std::uint64_t page_size=0;
    std::uint32_t baseline_ms=0, action_ms=0, post_ms=0, focus_poll_ms=0;
    std::size_t baseline_count=0;
    struct ProcessTiming {
        std::int32_t pid=0;
        std::uint64_t clear_start_ns=0, clear_end_ns=0, sample_start_ns=0, sample_end_ns=0;
    };
    struct PhaseTiming {
        std::string phase;
        std::size_t index=0;
        std::uint64_t start_ns=0, target_end_ns=0, end_ns=0;
        std::vector<ProcessTiming> processes;
    };
    std::vector<PhaseTiming> phases;
};
std::string EventJson(const RecognizedEvent&);
struct RuntimeHealth {
    bool started=false, running=false;
    std::uint64_t completed_windows=0, failed_windows=0;
    RuntimeCode last_code=RuntimeCode::Ok;
    std::string last_error, active_window;
};
std::string HealthJson(const RuntimeHealth&);

// Override Focus() for the verified host focus API. Procfs and collection are
// already implemented; override only if the target platform lacks that API.
// Callbacks run synchronously on the controlled-window caller and must return
// promptly. Stage receives baseline/action/post, index, and monotonic timestamp;
// it never receives truth labels. Publish returns true when a host output
// channel handled the JSON record; otherwise output_jsonl is used.
class PlatformBridge {
public:
    virtual ~PlatformBridge() = default;
    virtual FocusEvidence Focus()=0;
    virtual hc_process_identity Identity(std::int32_t pid);
    virtual void Clear(const hc_process_identity&);
    virtual FeatureMap Sample(const hc_process_identity&, const std::string& schema);
    virtual std::uint64_t SamplePageSize() const { return sampled_page_size_; }
    virtual void Stage(const std::string&, std::size_t, std::uint64_t) {}
    virtual bool Publish(const std::string&) { return false; }
protected:
    std::uint64_t sampled_page_size_=0;
};

// Start loads and verifies a model. RunControlled performs exactly one full
// baseline/action/post sequence after an independent, unlabeled trigger. It is
// blocking, single-flight, and may run on a host worker thread. Stop is safe from
// another thread: it interrupts waits and waits for the active call to unwind.
// Do not call Stop from a synchronous callback; use RequestStop there.
class ServiceRuntime {
public:
    ServiceRuntime(RuntimeConfig config, PlatformBridge& bridge);
    ~ServiceRuntime();
    void Start();
    RecognizedEvent RunControlled(const std::string& window_id, const std::string& event_id);
    void RequestStop() noexcept;
    void Stop();
    RuntimeHealth Health() const;
    std::vector<RecognizedEvent> History() const;
private:
    RuntimeConfig config_;
    PlatformBridge& bridge_;
    std::unique_ptr<CentroidModel> model_;
    mutable std::mutex mutex_;
    std::condition_variable condition_;
    std::atomic<bool> cancel_{false};
    RuntimeHealth health_;
    std::deque<RecognizedEvent> history_;
    std::deque<std::string> seen_order_;
    std::set<std::string> seen_;
    std::string history_instance_;
};

} // namespace harmony_ops
