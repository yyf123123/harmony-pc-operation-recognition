#include "harmony_ops/runtime.hpp"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <fstream>
#include <iomanip>
#include <limits>
#include <sstream>

namespace harmony_ops {
namespace {
bool IsWps(const std::string& schema) { return schema == SpecFor("wps").feature_schema; }
void CheckSchema(const std::string& schema) {
    if (!IsWps(schema) && schema != SpecFor("bilibili").feature_schema &&
        schema != SpecFor("huawei_browser").feature_schema)
        throw std::invalid_argument("unsupported raw-window feature schema: " + schema);
}
std::string Lower(std::string text) {
    for (auto& c : text) c=static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    return text;
}
void ValidateFeatureMap(const FeatureMap& map) {
    for (const auto& [key,value] : map)
        if ((key.first!="FILE" && key.first!="ANON") || !std::isfinite(value) || value<0)
            throw std::invalid_argument("invalid raw referenced-page feature");
}
double Median(std::vector<double> values) {
    std::sort(values.begin(),values.end());
    const auto n=values.size();
    return n%2 ? values[n/2] : (values[n/2-1]+values[n/2])/2;
}
RuntimeError CollectorError(const hc_error& error) {
    return RuntimeError(error.code==HC_PROCESS_CHANGED ? RuntimeCode::ProcessChanged : RuntimeCode::CollectionFailed,
        std::string(hc_status_name(error.code))+": "+error.message);
}
std::vector<std::int32_t> Coverage(const FocusEvidence& f) {
    auto pids=f.covered_pids;
    std::sort(pids.begin(),pids.end());
    if (f.root_pid<=0 || pids.empty() || pids.front()<=0 ||
        std::adjacent_find(pids.begin(),pids.end())!=pids.end() ||
        !std::binary_search(pids.begin(),pids.end(),f.root_pid) ||
        f.app_id.empty() || f.window_id.empty() || f.evidence_source.empty())
        throw RuntimeError(RuntimeCode::InvalidArgument,"incomplete focus evidence or process coverage");
    return pids;
}
bool SameIdentity(const hc_process_identity& a,const hc_process_identity& b) {
    return a.pid==b.pid && a.start_ticks==b.start_ticks && a.start_ticks!=0;
}
void AppendOutput(const std::filesystem::path& file,const std::string& json) {
    if (file.empty()) throw RuntimeError(RuntimeCode::OutputFailed,"no output channel configured");
    if (!file.parent_path().empty()) std::filesystem::create_directories(file.parent_path());
    std::ofstream output(file,std::ios::binary|std::ios::app);
    if (!output || !(output << json << '\n') || !output.flush())
        throw RuntimeError(RuntimeCode::OutputFailed,"JSONL output failed: "+file.string());
    output.close();
    if (!output) throw RuntimeError(RuntimeCode::OutputFailed,"JSONL output close failed: "+file.string());
}
} // namespace

std::uint64_t MonotonicNowNs() {
    return static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count());
}
std::string JsonString(const std::string& text) {
    std::ostringstream out;
    out << '"';
    for (unsigned char c : text) {
        if (c=='"' || c=='\\') out << '\\' << c;
        else if (c<32) out << "\\u" << std::hex << std::setw(4) << std::setfill('0') << unsigned(c) << std::dec;
        else out << c;
    }
    out << '"';
    return out.str();
}
FeatureMap SemanticFeatures(const hc_snapshot& snapshot,const std::string& schema) {
    CheckSchema(schema);
    if (!snapshot.page_size || !snapshot.vmas || !snapshot.count)
        throw std::invalid_argument("incomplete raw VMA snapshot");
    const bool wps=IsWps(schema);
    FeatureMap map;
    for (std::size_t i=0;i<snapshot.count;++i) {
        const auto& row=snapshot.vmas[i];
        // Fixed-size collector strings must always be terminated, including a
        // caller-supplied snapshot. Do not read beyond the public C buffers.
        const auto str=[](const char* data,std::size_t capacity) {
            const auto end=std::find(data,data+capacity,'\0');
            if (end==data+capacity) throw std::invalid_argument("unterminated VMA text field");
            return std::string(data,end);
        };
        const std::string original=str(row.pathname,sizeof(row.pathname));
        const auto segment=str(row.segment,sizeof(row.segment));
        const auto perms=str(row.permissions,sizeof(row.permissions));
        if (segment.empty() || perms.size()!=4) throw std::invalid_argument("invalid VMA segment/permissions");
        // Namespace is chosen before WPS path normalization, matching Python.
        const bool file=(!original.empty() && original.front()=='/') ||
            Lower(segment).rfind(wps?"filepage":"file",0)==0;
        auto path=original;
        std::replace(path.begin(),path.end(),'\\','/');
        if (wps) {
            const auto first=path.find_first_not_of(" \t\r\n\f\v"),last=path.find_last_not_of(" \t\r\n\f\v");
            path=first==std::string::npos?"":path.substr(first,last-first+1);
            auto end=std::unique(path.begin(),path.end(),[](char a,char b){return a=='/'&&b=='/';});
            path.erase(end,path.end());
        }
        if (path.empty()) path="(anonymous)";
        const double pages=wps?static_cast<double>(row.referenced_pages):
            static_cast<double>(row.referenced_bytes)/static_cast<double>(snapshot.page_size);
        const auto key=FeatureKey{file?"FILE":"ANON",path+"|segment="+segment+"|perms="+perms};
        auto& total=map[key]; total+=pages;
        if (!std::isfinite(total)) throw std::invalid_argument("VMA page aggregation overflow");
    }
    return map;
}
FeatureMap BaselineMedian(const std::vector<FeatureMap>& baselines) {
    if (baselines.empty()) throw std::invalid_argument("baseline windows are required");
    FeatureMap keys,result;
    for (const auto& map:baselines) { ValidateFeatureMap(map); keys.insert(map.begin(),map.end()); }
    for (const auto& [key,unused]:keys) {
        (void)unused;
        std::vector<double> values;
        for (const auto& map:baselines) {
            const auto it=map.find(key);
            values.push_back(it==map.end()?0:it->second);
        }
        result[key]=Median(std::move(values));
    }
    return result;
}
FeatureMap ExcessPages(const std::vector<FeatureMap>& baselines,const FeatureMap& action,const FeatureMap& post) {
    const auto median=BaselineMedian(baselines);
    ValidateFeatureMap(action); ValidateFeatureMap(post);
    auto keys=action; keys.insert(post.begin(),post.end());
    FeatureMap excess;
    for (const auto& [key,unused]:keys) {
        (void)unused;
        const auto a=action.find(key),p=post.find(key),b=median.find(key);
        const double value=std::max(a==action.end()?0:a->second,p==post.end()?0:p->second)-(b==median.end()?0:b->second);
        if (value>0) excess[key]=value;
    }
    return excess;
}
std::vector<double> BuildWindowVector(const std::string& schema,const std::vector<FeatureMap>& baselines,
                                      const FeatureMap& action,const FeatureMap& post) {
    CheckSchema(schema);
    return IsWps(schema)?BuildWpsVector(ExcessPages(baselines,action,post)):
        BuildCrossAppV2Vector(baselines,action,post);
}
const char* RuntimeCodeName(RuntimeCode code) noexcept {
    switch(code) {
        case RuntimeCode::Ok:return "ok";
        case RuntimeCode::InvalidArgument:return "invalid_argument";
        case RuntimeCode::NotStarted:return "not_started";
        case RuntimeCode::Busy:return "busy";
        case RuntimeCode::Cancelled:return "cancelled";
        case RuntimeCode::FocusChanged:return "focus_changed";
        case RuntimeCode::ProcessChanged:return "process_changed";
        case RuntimeCode::CollectionFailed:return "collection_failed";
        case RuntimeCode::SamplingTimeout:return "sampling_timeout";
        case RuntimeCode::ModelError:return "model_error";
        case RuntimeCode::OutputFailed:return "output_failed";
        case RuntimeCode::DuplicateEvent:return "duplicate_event";
        default:return "internal_error";
    }
}
std::string EventJson(const RecognizedEvent& event) {
    std::ostringstream out;
    out << std::setprecision(17) << "{\"schema\":\"harmony.ops.recognized.v1\",\"status\":\"success\",\"method\":\"vma-centroid\",\"source\":\"controlled_live_window\",\"event_id\":" << JsonString(event.event_id)
        << ",\"window_id\":" << JsonString(event.window_id) << ",\"session_id\":" << JsonString(event.session_id)
        << ",\"app_id\":" << JsonString(event.app_id) << ",\"app_instance_id\":" << JsonString(event.app_instance_id)
        << ",\"coverage_id\":" << JsonString(event.coverage_id) << ",\"focus_window_id\":" << JsonString(event.focus_window_id)
        << ",\"evidence_source\":" << JsonString(event.evidence_source) << ",\"clock\":\"monotonic_ns\",\"start_ns\":" << event.start_ns
        << ",\"end_ns\":" << event.end_ns << ",\"operation_label\":" << JsonString(event.prediction.operation_label)
        << ",\"cosine_similarity\":" << event.prediction.cosine_similarity << ",\"center_id\":" << event.prediction.center_id
        << ",\"feature_schema\":" << JsonString(SpecFor(event.app_id).feature_schema) << ",\"processes\":[";
    bool first=true;
    for (const auto& process:event.processes) {
        if (!first) out << ',';
        first=false;
        out << "{\"pid\":" << process.pid << ",\"start_ticks\":" << process.start_ticks << '}';
    }
    out << "],\"page_size\":" << event.page_size
        << ",\"capture_profile\":{\"schema\":\"harmony.ops.controlled-window.v1\",\"baseline_ms\":" << event.baseline_ms
        << ",\"baseline_count\":" << event.baseline_count << ",\"action_ms\":" << event.action_ms
        << ",\"post_ms\":" << event.post_ms << ",\"focus_poll_ms\":" << event.focus_poll_ms
        << ",\"focus_validation\":\"polled\",\"autonomous_segmentation\":false},\"phases\":[";
    first=true;
    for (const auto& phase:event.phases) {
        if (!first) out << ',';
        first=false;
        out << "{\"phase\":" << JsonString(phase.phase) << ",\"index\":" << phase.index
            << ",\"start_ns\":" << phase.start_ns << ",\"target_end_ns\":" << phase.target_end_ns
            << ",\"end_ns\":" << phase.end_ns << ",\"processes\":[";
        bool first_process=true;
        for (const auto& process:phase.processes) {
            if (!first_process) out << ',';
            first_process=false;
            out << "{\"pid\":" << process.pid << ",\"clear_start_ns\":" << process.clear_start_ns
                << ",\"clear_end_ns\":" << process.clear_end_ns << ",\"sample_start_ns\":" << process.sample_start_ns
                << ",\"sample_end_ns\":" << process.sample_end_ns << '}';
        }
        out << "]}";
    }
    return out.str()+"]}";
}
std::string HealthJson(const RuntimeHealth& health) {
    std::ostringstream out;
    out << "{\"schema\":\"harmony.ops.health.v1\",\"started\":" << (health.started?"true":"false")
        << ",\"running\":" << (health.running?"true":"false") << ",\"completed_windows\":" << health.completed_windows
        << ",\"failed_windows\":" << health.failed_windows << ",\"last_code\":" << JsonString(RuntimeCodeName(health.last_code))
        << ",\"last_error\":" << JsonString(health.last_error) << ",\"active_window\":" << JsonString(health.active_window) << '}';
    return out.str();
}
hc_process_identity PlatformBridge::Identity(std::int32_t pid) {
    hc_process_identity identity{}; hc_error error{};
    if (hc_read_identity(pid,&identity,&error)!=HC_OK) throw CollectorError(error);
    return identity;
}
void PlatformBridge::Clear(const hc_process_identity& process) {
    hc_error error{};
    if (hc_clear_refs(&process,&error)!=HC_OK) throw CollectorError(error);
}
FeatureMap PlatformBridge::Sample(const hc_process_identity& process,const std::string& schema) {
    hc_snapshot snapshot{}; hc_error error{};
    if (hc_collect(&process,IsWps(schema)?HC_SEGMENT_WPS:HC_SEGMENT_CROSS_APP,&snapshot,&error)!=HC_OK)
        throw CollectorError(error);
    try {
        sampled_page_size_=snapshot.page_size;
        auto map=SemanticFeatures(snapshot,schema);
        hc_snapshot_free(&snapshot);
        return map;
    } catch (...) { hc_snapshot_free(&snapshot); throw; }
}
ServiceRuntime::ServiceRuntime(RuntimeConfig config,PlatformBridge& bridge)
    :config_(std::move(config)),bridge_(bridge) {}
ServiceRuntime::~ServiceRuntime() { Stop(); }
void ServiceRuntime::Start() {
    std::lock_guard<std::mutex> lock(mutex_);
    if (health_.started || health_.running) throw RuntimeError(RuntimeCode::Busy,"runtime already started");
    if (config_.session_id.empty() || !config_.baseline_ms || !config_.action_ms || !config_.post_ms ||
        config_.baseline_count<2 || config_.baseline_count>1024 || !config_.history_limit ||
        config_.dedup_limit<config_.history_limit || !config_.max_sample_ms || !config_.focus_poll_ms || config_.focus_poll_ms>1000)
        throw RuntimeError(RuntimeCode::InvalidArgument,"invalid runtime configuration");
    try { model_=std::make_unique<CentroidModel>(CentroidModel::Load(config_.models_root,config_.app_id,SpecFor(config_.app_id).feature_schema)); }
    catch (const std::exception& error) {
        health_.last_code=RuntimeCode::ModelError; health_.last_error=error.what();
        throw RuntimeError(RuntimeCode::ModelError,error.what());
    }
    history_.clear(); seen_.clear(); seen_order_.clear(); history_instance_.clear();
    cancel_=false; health_=RuntimeHealth{}; health_.started=true;
}
void ServiceRuntime::RequestStop() noexcept { cancel_=true; condition_.notify_all(); }
void ServiceRuntime::Stop() {
    RequestStop();
    std::unique_lock<std::mutex> lock(mutex_);
    condition_.wait(lock,[&]{return !health_.running;});
    health_.started=false; model_.reset();
}
RuntimeHealth ServiceRuntime::Health() const { std::lock_guard<std::mutex> lock(mutex_); return health_; }
std::vector<RecognizedEvent> ServiceRuntime::History() const {
    std::lock_guard<std::mutex> lock(mutex_); return {history_.begin(),history_.end()};
}
RecognizedEvent ServiceRuntime::RunControlled(const std::string& window,const std::string& event_id) {
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (!health_.started) throw RuntimeError(RuntimeCode::NotStarted,"runtime is not started");
        if (health_.running) throw RuntimeError(RuntimeCode::Busy,"a controlled window is already running");
        if (window.empty() || event_id.empty()) throw RuntimeError(RuntimeCode::InvalidArgument,"window/event ID must be nonempty");
        if (seen_.count(event_id)) throw RuntimeError(RuntimeCode::DuplicateEvent,"recognized event ID was already published");
        health_.running=true; health_.active_window=window;
    }
    const auto finish=[&](RuntimeCode code,const std::string& message) {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            health_.last_code=code; health_.last_error=message;
            if (code==RuntimeCode::Ok) ++health_.completed_windows;
            else { ++health_.failed_windows; history_.clear(); history_instance_.clear(); }
        }
        if (code!=RuntimeCode::Ok) {
            // A failure record carries no operation prediction. Preserve the
            // original fault if its diagnostic output is also unavailable.
            try {
                const auto json=std::string("{\"schema\":\"harmony.ops.error.v1\",\"status\":\"error\",\"window_id\":")+
                    JsonString(window)+",\"event_id\":"+JsonString(event_id)+",\"code\":"+
                    JsonString(RuntimeCodeName(code))+",\"message\":"+JsonString(message)+"}";
                if (!bridge_.Publish(json)) AppendOutput(config_.output_jsonl,json);
            } catch (...) {}
        }
        {
            std::lock_guard<std::mutex> lock(mutex_);
            health_.running=false; health_.active_window.clear();
        }
        condition_.notify_all();
    };
    try {
        const auto cancelled=[&] { if (cancel_) throw RuntimeError(RuntimeCode::Cancelled,"controlled window cancelled"); };
        cancelled();
        const auto initial=bridge_.Focus();
        const auto pids=Coverage(initial);
        if (initial.app_id!=config_.app_id) throw RuntimeError(RuntimeCode::FocusChanged,"focused application does not match configured model");
        RecognizedEvent event;
        event.event_id=event_id; event.window_id=window; event.session_id=config_.session_id;
        event.app_id=config_.app_id; event.focus_window_id=initial.window_id; event.evidence_source=initial.evidence_source;
        event.start_ns=MonotonicNowNs();
        event.baseline_ms=config_.baseline_ms; event.baseline_count=config_.baseline_count;
        event.action_ms=config_.action_ms; event.post_ms=config_.post_ms; event.focus_poll_ms=config_.focus_poll_ms;
        hc_process_identity root{};
        std::string coverage_material;
        for (const auto pid:pids) {
            const auto identity=bridge_.Identity(pid);
            if (identity.pid!=pid || !identity.start_ticks) throw RuntimeError(RuntimeCode::ProcessChanged,"invalid process start identity");
            event.processes.push_back(identity);
            if (pid==initial.root_pid) root=identity;
            coverage_material+=std::to_string(pid)+":"+std::to_string(identity.start_ticks)+";";
        }
        event.app_instance_id=config_.app_id+":"+std::to_string(root.pid)+":"+std::to_string(root.start_ticks);
        event.coverage_id=Sha256Hex(coverage_material);
        const auto validate=[&] {
            cancelled();
            const auto focus=bridge_.Focus();
            if (focus.app_id!=initial.app_id || focus.window_id!=initial.window_id || focus.root_pid!=initial.root_pid ||
                focus.evidence_source!=initial.evidence_source || Coverage(focus)!=pids)
                throw RuntimeError(RuntimeCode::FocusChanged,"focus, root process, or child coverage changed");
            for (const auto& process:event.processes)
                if (!SameIdentity(process,bridge_.Identity(process.pid)))
                    throw RuntimeError(RuntimeCode::ProcessChanged,"PID start identity changed");
            cancelled();
        };
        const auto capture=[&](const std::string& phase,std::size_t index,std::uint32_t duration_ms) {
            validate();
            const auto clear_start=MonotonicNowNs();
            RecognizedEvent::PhaseTiming timing;
            timing.phase=phase; timing.index=index;
            for (const auto& process:event.processes) {
                cancelled();
                RecognizedEvent::ProcessTiming pt;
                pt.pid=process.pid; pt.clear_start_ns=MonotonicNowNs();
                bridge_.Clear(process); pt.clear_end_ns=MonotonicNowNs();
                timing.processes.push_back(pt);
            }
            const auto start=MonotonicNowNs();
            if (start-clear_start>std::uint64_t(config_.max_sample_ms)*1000000)
                throw RuntimeError(RuntimeCode::SamplingTimeout,"clear_refs phase exceeded sampling budget");
            const auto deadline=start+std::uint64_t(duration_ms)*1000000;
            timing.start_ns=start; timing.target_end_ns=deadline;
            bridge_.Stage(phase,index,start);
            if (MonotonicNowNs()>deadline)
                throw RuntimeError(RuntimeCode::SamplingTimeout,"phase callback exceeded the observation interval");
            while (MonotonicNowNs()<deadline) {
                validate();
                const auto poll=std::min(deadline,MonotonicNowNs()+std::uint64_t(config_.focus_poll_ms)*1000000);
                std::unique_lock<std::mutex> lock(mutex_);
                condition_.wait_until(lock,std::chrono::steady_clock::time_point(std::chrono::nanoseconds(poll)),[&]{return cancel_.load();});
            }
            validate();
            FeatureMap combined;
            for (std::size_t i=0;i<event.processes.size();++i) {
                const auto& process=event.processes[i];
                cancelled();
                timing.processes[i].sample_start_ns=MonotonicNowNs();
                const auto map=bridge_.Sample(process,model_->spec().feature_schema);
                timing.processes[i].sample_end_ns=MonotonicNowNs();
                const auto page_size=bridge_.SamplePageSize();
                if (page_size<1024 || (page_size&(page_size-1)) || (event.page_size && event.page_size!=page_size))
                    throw RuntimeError(RuntimeCode::CollectionFailed,"missing, invalid, or inconsistent actual page size");
                event.page_size=page_size;
                if (map.empty()) throw RuntimeError(RuntimeCode::CollectionFailed,"empty process feature snapshot");
                ValidateFeatureMap(map);
                for (const auto& [key,value]:map) combined[key]+=value;
            }
            validate();
            if (MonotonicNowNs()>deadline+std::uint64_t(config_.max_sample_ms)*1000000)
                throw RuntimeError(RuntimeCode::SamplingTimeout,"sampling phase exceeded its deadline");
            timing.end_ns=MonotonicNowNs(); event.phases.push_back(std::move(timing));
            return combined;
        };
        std::vector<FeatureMap> baselines;
        for (std::size_t i=0;i<config_.baseline_count;++i) baselines.push_back(capture("baseline",i,config_.baseline_ms));
        const auto action=capture("action",0,config_.action_ms);
        const auto post=capture("post",0,config_.post_ms);
        const auto vector=BuildWindowVector(model_->spec().feature_schema,baselines,action,post);
        double norm=0;
        for (double value:vector) norm+=value*value;
        if (!(norm>0) || !std::isfinite(norm)) throw RuntimeError(RuntimeCode::CollectionFailed,"zero or non-finite window feature");
        event.prediction=model_->Predict(vector); event.end_ns=MonotonicNowNs();
        validate();
        try { if (!bridge_.Publish(EventJson(event))) AppendOutput(config_.output_jsonl,EventJson(event)); }
        catch (const std::exception& error) { throw RuntimeError(RuntimeCode::OutputFailed,error.what()); }
        {
            std::lock_guard<std::mutex> lock(mutex_);
            if (history_instance_!=event.app_instance_id) { history_.clear(); history_instance_=event.app_instance_id; }
            history_.push_back(event);
            while (history_.size()>config_.history_limit) history_.pop_front();
            seen_.insert(event.event_id); seen_order_.push_back(event.event_id);
            while (seen_order_.size()>config_.dedup_limit) { seen_.erase(seen_order_.front()); seen_order_.pop_front(); }
        }
        finish(RuntimeCode::Ok,""); return event;
    } catch (const RuntimeError& error) { finish(error.code(),error.what()); throw; }
      catch (const std::exception& error) { finish(RuntimeCode::CollectionFailed,error.what()); throw RuntimeError(RuntimeCode::CollectionFailed,error.what()); }
      catch (...) { finish(RuntimeCode::InternalError,"unknown platform failure"); throw RuntimeError(RuntimeCode::InternalError,"unknown platform failure"); }
}
} // namespace harmony_ops
