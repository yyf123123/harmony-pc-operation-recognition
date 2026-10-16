#include "harmony_ops/runtime.hpp"

#include <atomic>
#include <fstream>
#include <iostream>
#include <sstream>
#include <thread>

using namespace harmony_ops;
namespace {
void Require(bool value,const char* why) { if (!value) throw std::runtime_error(why); }
template<class F> void Expect(RuntimeCode code,F function) {
    try { function(); } catch (const RuntimeError& error) { Require(error.code()==code,"wrong runtime error code"); return; }
    throw std::runtime_error("expected runtime failure");
}
FeatureMap Raw(const std::filesystem::path& file,const std::string& schema) {
    std::ifstream input(file,std::ios::binary); std::ostringstream text; text << input.rdbuf();
    Require(bool(input),"fixture read failed");
    const auto bytes=text.str(); hc_snapshot snapshot{}; hc_error error{};
    Require(hc_parse_smaps(bytes.data(),bytes.size(),4096,schema==SpecFor("wps").feature_schema?HC_SEGMENT_WPS:HC_SEGMENT_CROSS_APP,&snapshot,&error)==HC_OK,"fixture parse failed");
    try { auto map=SemanticFeatures(snapshot,schema); hc_snapshot_free(&snapshot); return map; }
    catch (...) { hc_snapshot_free(&snapshot); throw; }
}
struct FakeBridge : PlatformBridge {
    enum Fault { None,FocusMove,CoverageChange,Restart,Partial,Permission,Missing,Timeout,Output } fault=None;
    std::vector<FeatureMap> baseline;
    FeatureMap action,post;
    std::string phase;
    std::size_t index=0;
    std::uint64_t root_ticks=100;
    std::vector<std::string> published;
    std::atomic<bool> stage_entered{false};
    bool publish_to_file=false;
    FocusEvidence Focus() override {
        FocusEvidence focus{"wps","window-A","synthetic-test-provider",10,{10,11}};
        if (phase=="action" && fault==FocusMove) focus.window_id="window-B";
        if (phase=="action" && fault==CoverageChange) focus.covered_pids.push_back(12);
        return focus;
    }
    hc_process_identity Identity(std::int32_t pid) override {
        if (fault==Permission) throw RuntimeError(RuntimeCode::CollectionFailed,"synthetic permission denied");
        return {pid,pid==10?root_ticks+(phase=="action" && fault==Restart?1u:0u):200u};
    }
    void Clear(const hc_process_identity&) override {}
    std::uint64_t SamplePageSize() const override { return 4096; }
    FeatureMap Sample(const hc_process_identity& process,const std::string&) override {
        if (phase=="action" && ((fault==Partial && process.pid==11) || fault==Missing))
            throw RuntimeError(RuntimeCode::CollectionFailed,"synthetic required process/Referenced missing");
        return phase=="baseline"?baseline.at(index):phase=="action"?action:post;
    }
    void Stage(const std::string& next,std::size_t n,std::uint64_t) override {
        phase=next; index=n; stage_entered=true;
        if (fault==Timeout && next=="action") std::this_thread::sleep_for(std::chrono::milliseconds(20));
    }
    bool Publish(const std::string& json) override {
        if (fault==Output) throw std::runtime_error("synthetic output refused");
        if (publish_to_file) return false;
        published.push_back(json); return true;
    }
    void Reset() { phase.clear(); index=0; stage_entered=false; published.clear(); }
};
RuntimeConfig Config(const std::filesystem::path& models) {
    RuntimeConfig config; config.models_root=models; config.session_id="synthetic-session";
    config.baseline_ms=1; config.action_ms=1; config.post_ms=1; config.focus_poll_ms=1;
    config.max_sample_ms=50; config.history_limit=2; config.dedup_limit=8;
    return config;
}
std::size_t Successes(const FakeBridge& bridge) {
    std::size_t count=0;
    for (const auto& line:bridge.published) if (line.find("\"status\":\"success\"")!=std::string::npos) ++count;
    return count;
}
} // namespace
int main(int argc,char** argv) {
    try {
        Require(argc==3,"usage: runtime_test MODELS RAW_FIXTURES");
        FakeBridge bridge;
        const auto schema=SpecFor("wps").feature_schema;
        bridge.baseline={Raw(std::filesystem::path(argv[2])/"baseline0.smaps",schema),Raw(std::filesystem::path(argv[2])/"baseline1.smaps",schema)};
        bridge.action=Raw(std::filesystem::path(argv[2])/"action.smaps",schema);
        bridge.post=Raw(std::filesystem::path(argv[2])/"post.smaps",schema);
        auto config=Config(argv[1]);
        ServiceRuntime runtime(config,bridge);
        Expect(RuntimeCode::NotStarted,[&]{runtime.RunControlled("w","e");});
        runtime.Start();
        const auto first=runtime.RunControlled("w1","e1");
        Require(first.app_instance_id=="wps:10:100","root identity must define application instance");
        Require(first.page_size==4096 && first.phases.size()==4 && first.phases[0].processes.size()==2,"actual page size and per-process phase timing required");
        bridge.Reset();
        const auto second=runtime.RunControlled("w2","e2");
        Require(first.prediction.operation_label==second.prediction.operation_label,"repeated actions fixture changed prediction");
        Require(runtime.History().size()==2,"repeated labels must be retained");
        Expect(RuntimeCode::DuplicateEvent,[&]{runtime.RunControlled("w3","e2");});
        bridge.Reset(); runtime.RunControlled("w3","e3");
        Require(runtime.History().size()==2 && runtime.History()[0].event_id=="e2","bounded history order");
        bridge.root_ticks=300; bridge.Reset(); runtime.RunControlled("w4","e4");
        Require(runtime.History().size()==1 && runtime.History()[0].app_instance_id=="wps:10:300","restart resets history");
        runtime.Stop(); runtime.Start();
        Require(runtime.History().empty(),"service restart must clear history");
        bridge.Reset(); runtime.RunControlled("w5","e1");
        Require(Successes(bridge)==1,"service restart must clear dedup state");
        bridge.fault=FakeBridge::FocusMove;
        bridge.Reset();
        Expect(RuntimeCode::FocusChanged,[&]{runtime.RunControlled("gap-window","gap-event");});
        Require(runtime.History().empty() && Successes(bridge)==0,"unknown gap must reset history");
        Require(bridge.published.back().find("operation_label")==std::string::npos,"failure event must not carry a predicted label");
        runtime.Stop();
        const std::vector<std::pair<FakeBridge::Fault,RuntimeCode>> faults{
            {FakeBridge::FocusMove,RuntimeCode::FocusChanged},{FakeBridge::CoverageChange,RuntimeCode::FocusChanged},
            {FakeBridge::Restart,RuntimeCode::ProcessChanged},{FakeBridge::Partial,RuntimeCode::CollectionFailed},
            {FakeBridge::Permission,RuntimeCode::CollectionFailed},{FakeBridge::Missing,RuntimeCode::CollectionFailed},
            {FakeBridge::Timeout,RuntimeCode::SamplingTimeout},{FakeBridge::Output,RuntimeCode::OutputFailed}};
        for (const auto& [fault,expected]:faults) {
            bridge.Reset(); bridge.fault=fault;
            auto test_config=config; test_config.max_sample_ms=fault==FakeBridge::Timeout?2:50;
            ServiceRuntime failed(test_config,bridge); failed.Start();
            Expect(expected,[&]{failed.RunControlled("bad-window","bad-event");});
            Require(failed.History().empty() && Successes(bridge)==0,"failed window must not predict or update history");
            Require(!failed.Health().running && failed.Health().failed_windows==1,"failure cleanup/health");
            if (fault!=FakeBridge::Output) Require(!bridge.published.empty() && bridge.published.back().find("\"status\":\"error\"")!=std::string::npos,"structured error record required");
        }
        bridge.Reset(); bridge.fault=FakeBridge::None;
        const auto saved_action=bridge.action, saved_post=bridge.post;
        bridge.action={{{"ANON","synthetic-zero"},0}}; bridge.post=bridge.action;
        ServiceRuntime zero(config,bridge); zero.Start();
        Expect(RuntimeCode::CollectionFailed,[&]{zero.RunControlled("zero-window","zero-event");});
        Require(zero.History().empty() && Successes(bridge)==0,"zero vector must not generate valid prediction");
        bridge.action=saved_action; bridge.post=saved_post; bridge.Reset();
        auto cancel_config=config; cancel_config.baseline_ms=5000;
        ServiceRuntime cancellable(cancel_config,bridge); cancellable.Start();
        std::atomic<bool> cancelled{false};
        std::thread worker([&]{try {cancellable.RunControlled("cancel-window","cancel-event");} catch (const RuntimeError& error) {cancelled=error.code()==RuntimeCode::Cancelled;}});
        while (!bridge.stage_entered.load()) std::this_thread::yield();
        Expect(RuntimeCode::Busy,[&]{cancellable.RunControlled("parallel","parallel");});
        const auto stop_start=MonotonicNowNs(); cancellable.Stop(); worker.join();
        Require(cancelled && MonotonicNowNs()-stop_start<500000000,"Stop must interrupt long waits");
        Require(!cancellable.Health().started && cancellable.History().empty(),"stop health/history");
        auto bad_config=config; bad_config.models_root="/nonexistent/synthetic/models";
        ServiceRuntime bad_model(bad_config,bridge);
        Expect(RuntimeCode::ModelError,[&]{bad_model.Start();});
        Require(!bad_model.Health().started,"invalid model must not start");
        auto invalid_config=config; invalid_config.baseline_count=1;
        ServiceRuntime invalid(invalid_config,bridge);
        Expect(RuntimeCode::InvalidArgument,[&]{invalid.Start();});
        // Semantic namespaces/path normalization are frozen separately.
        const std::string text="1000-2000 r--p 00000000 00:00 1 /test//a.so\nReferenced: 4 kB\n";
        hc_snapshot wps{},cross{}; hc_error error{};
        Require(hc_parse_smaps(text.data(),text.size(),16384,HC_SEGMENT_WPS,&wps,&error)==HC_OK,"16KiB WPS fixture");
        Require(hc_parse_smaps(text.data(),text.size(),16384,HC_SEGMENT_CROSS_APP,&cross,&error)==HC_OK,"16KiB cross fixture");
        const auto wm=SemanticFeatures(wps,schema),cm=SemanticFeatures(cross,SpecFor("bilibili").feature_schema);
        hc_snapshot_free(&wps); hc_snapshot_free(&cross);
        Require(wm.begin()->first.second=="/test/a.so|segment=.so|perms=r--p" && wm.begin()->second==1,"WPS semantics/actual pages");
        Require(cm.begin()->first.second=="/test//a.so|segment=FilePage other|perms=r--p" && cm.begin()->second==0.25,"cross-app semantics/actual pages");
        std::cout << "runtime tests passed: history, failures, cancellation, raw semantics\n";
        return 0;
    } catch (const std::exception& error) { std::cerr << error.what() << '\n'; return 1; }
}
