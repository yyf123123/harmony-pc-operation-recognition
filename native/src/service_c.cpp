#include "harmony_ops/service.h"
#include "harmony_ops/runtime.hpp"
#include "harmony_ops/replay.hpp"

#include <algorithm>
#include <cstring>
#include <memory>
#include <new>
#include <iomanip>
#include <sstream>

using namespace harmony_ops;
namespace {
std::string String(const char* value) { return value?value:""; }
template<std::size_t N> std::string Text(const char (&value)[N]) {
    const auto end=std::find(value,value+N,'\0');
    if (end==value+N) throw RuntimeError(RuntimeCode::InvalidArgument,"unterminated host callback field");
    return {value,end};
}
class CallbackBridge final : public PlatformBridge {
public:
    hop_callbacks callbacks{};
    FocusEvidence Focus() override {
        if (!callbacks.focus) throw RuntimeError(RuntimeCode::FocusChanged,"host focus callback is not connected");
        hop_focus focus{};
        if (callbacks.focus(callbacks.context,&focus)) throw RuntimeError(RuntimeCode::FocusChanged,"host focus callback failed");
        if (focus.pid_count>HOP_MAX_PIDS) throw RuntimeError(RuntimeCode::InvalidArgument,"host PID coverage exceeds ABI capacity");
        return {Text(focus.app_id),Text(focus.window_id),Text(focus.evidence_source),focus.root_pid,
            std::vector<std::int32_t>(focus.covered_pids,focus.covered_pids+focus.pid_count)};
    }
    hc_process_identity Identity(std::int32_t pid) override {
        if (!callbacks.identity) return PlatformBridge::Identity(pid);
        std::uint64_t ticks=0;
        if (callbacks.identity(callbacks.context,pid,&ticks) || !ticks)
            throw RuntimeError(RuntimeCode::ProcessChanged,"host process identity callback failed");
        return {pid,ticks};
    }
    void Clear(const hc_process_identity& process) override {
        if (!callbacks.clear_refs) { PlatformBridge::Clear(process); return; }
        if (callbacks.clear_refs(callbacks.context,process.pid,process.start_ticks))
            throw RuntimeError(RuntimeCode::CollectionFailed,"host clear_refs callback failed");
    }
    FeatureMap Sample(const hc_process_identity& process,const std::string& schema) override {
        if (!callbacks.read_smaps) return PlatformBridge::Sample(process,schema);
        std::size_t length=0; std::uint64_t page_size=0;
        if (callbacks.read_smaps(callbacks.context,process.pid,process.start_ticks,nullptr,0,&length,&page_size) ||
            !length || length>64u*1024u*1024u)
            throw RuntimeError(RuntimeCode::CollectionFailed,"host smaps size callback failed or exceeds 64 MiB");
        std::vector<char> buffer(length);
        const auto capacity=length;
        if (callbacks.read_smaps(callbacks.context,process.pid,process.start_ticks,buffer.data(),capacity,&length,&page_size) || length>capacity)
            throw RuntimeError(RuntimeCode::CollectionFailed,"host smaps read callback failed");
        hc_snapshot snapshot{}; hc_error error{};
        if (hc_parse_smaps(buffer.data(),length,page_size,schema==SpecFor("wps").feature_schema?HC_SEGMENT_WPS:HC_SEGMENT_CROSS_APP,&snapshot,&error)!=HC_OK)
            throw RuntimeError(RuntimeCode::CollectionFailed,std::string(hc_status_name(error.code))+": "+error.message);
        try { sampled_page_size_=snapshot.page_size; auto result=SemanticFeatures(snapshot,schema); hc_snapshot_free(&snapshot); return result; }
        catch (...) { hc_snapshot_free(&snapshot); throw; }
    }
    void Stage(const std::string& phase,std::size_t index,std::uint64_t ns) override {
        if (callbacks.stage && callbacks.stage(callbacks.context,phase.c_str(),index,ns))
            throw RuntimeError(RuntimeCode::CollectionFailed,"host phase callback failed");
    }
    bool Publish(const std::string& json) override {
        if (!callbacks.publish) return false;
        if (callbacks.publish(callbacks.context,json.data(),json.size()))
            throw RuntimeError(RuntimeCode::OutputFailed,"host output callback failed");
        return true;
    }
};
hop_status Error(hop_error* error,RuntimeCode code,const char* message) noexcept {
    const auto status=static_cast<hop_status>(code);
    if (error) {
        error->code=status;
        std::strncpy(error->message,message?message:"",sizeof(error->message)-1);
        error->message[sizeof(error->message)-1]='\0';
    }
    return status;
}
template<class F> hop_status Protect(hop_error* error,F function) noexcept {
    try { function(); return Error(error,RuntimeCode::Ok,""); }
    catch (const RuntimeError& failure) { return Error(error,failure.code(),failure.what()); }
    catch (const std::invalid_argument& failure) { return Error(error,RuntimeCode::InvalidArgument,failure.what()); }
    catch (const std::exception& failure) { return Error(error,RuntimeCode::InternalError,failure.what()); }
    catch (...) { return Error(error,RuntimeCode::InternalError,"unknown C ABI failure"); }
}
void Copy(const std::string& value,char* buffer,std::size_t capacity,std::size_t* required) {
    if (!required) throw std::invalid_argument("required byte-count pointer is null");
    *required=value.size()+1;
    if (!buffer && !capacity) return;
    if (!buffer || capacity<*required) throw std::invalid_argument("output buffer is too small");
    std::memcpy(buffer,value.c_str(),*required);
}
} // namespace

struct hop_handle {
    CallbackBridge bridge;
    std::unique_ptr<ServiceRuntime> runtime;
    std::filesystem::path models_root;
    std::string app_id;
};

extern "C" {
void hop_default_config(hop_config* out) {
    if (!out) return;
    *out={}; out->abi_version=HOP_ABI_VERSION; out->struct_size=sizeof(*out);
    out->app_id="wps"; out->baseline_ms=5000; out->action_ms=15000; out->post_ms=5000;
    out->max_sample_ms=1000; out->focus_poll_ms=100; out->baseline_count=2; out->history_limit=64; out->dedup_limit=1024;
}
void hop_default_callbacks(hop_callbacks* out) {
    if (!out) return;
    *out={}; out->abi_version=HOP_ABI_VERSION; out->struct_size=sizeof(*out);
}
hop_status hop_create(const hop_config* config,const hop_callbacks* callbacks,hop_handle** out,hop_error* error) {
    if (out) *out=nullptr;
    return Protect(error,[&] {
        if (!out || !config || !callbacks || config->abi_version!=HOP_ABI_VERSION || callbacks->abi_version!=HOP_ABI_VERSION ||
            config->struct_size!=sizeof(*config) || callbacks->struct_size!=sizeof(*callbacks))
            throw std::invalid_argument("unsupported C ABI version or structure size");
        RuntimeConfig runtime;
        runtime.models_root=String(config->models_root); runtime.app_id=String(config->app_id);
        runtime.session_id=String(config->session_id); runtime.output_jsonl=String(config->output_jsonl);
        runtime.baseline_ms=config->baseline_ms; runtime.action_ms=config->action_ms; runtime.post_ms=config->post_ms;
        runtime.max_sample_ms=config->max_sample_ms; runtime.focus_poll_ms=config->focus_poll_ms;
        runtime.baseline_count=config->baseline_count; runtime.history_limit=config->history_limit; runtime.dedup_limit=config->dedup_limit;
        auto handle=std::make_unique<hop_handle>();
        handle->bridge.callbacks=*callbacks;
        handle->models_root=runtime.models_root; handle->app_id=runtime.app_id;
        handle->runtime=std::make_unique<ServiceRuntime>(std::move(runtime),handle->bridge);
        *out=handle.release();
    });
}
hop_status hop_start(hop_handle* handle,hop_error* error) {
    return Protect(error,[&]{if (!handle) throw std::invalid_argument("null runtime handle"); handle->runtime->Start();});
}
hop_status hop_run(hop_handle* handle,const char* window,const char* event,hop_error* error) {
    return Protect(error,[&]{if (!handle) throw std::invalid_argument("null runtime handle"); handle->runtime->RunControlled(String(window),String(event));});
}
hop_status hop_request_stop(hop_handle* handle,hop_error* error) {
    return Protect(error,[&]{if (!handle) throw std::invalid_argument("null runtime handle"); handle->runtime->RequestStop();});
}
hop_status hop_stop(hop_handle* handle,hop_error* error) {
    return Protect(error,[&]{if (!handle) throw std::invalid_argument("null runtime handle"); handle->runtime->Stop();});
}
hop_status hop_health_json(hop_handle* handle,char* buffer,size_t capacity,size_t* required,hop_error* error) {
    return Protect(error,[&]{if (!handle) throw std::invalid_argument("null runtime handle"); Copy(HealthJson(handle->runtime->Health()),buffer,capacity,required);});
}
hop_status hop_history_json(hop_handle* handle,char* buffer,size_t capacity,size_t* required,hop_error* error) {
    return Protect(error,[&]{
        if (!handle) throw std::invalid_argument("null runtime handle");
        std::string json="[";
        for (const auto& event:handle->runtime->History()) { if (json.size()>1) json+=','; json+=EventJson(event); }
        Copy(json+"]",buffer,capacity,required);
    });
}
hop_status hop_replay(hop_handle* handle,const char* manifest,char* buffer,size_t capacity,size_t* required,hop_error* error) {
    return Protect(error,[&] {
        if (!handle || !manifest || !*manifest) throw std::invalid_argument("runtime handle and raw replay manifest are required");
        if (!handle->runtime->Health().started) throw RuntimeError(RuntimeCode::NotStarted,"runtime is not started");
        ReplayWindow replay;
        try { replay=LoadRawReplay(manifest,handle->app_id); }
        catch (const std::exception& failure) { throw RuntimeError(RuntimeCode::CollectionFailed,failure.what()); }
        Prediction prediction;
        try { prediction=CentroidModel::Load(handle->models_root,handle->app_id,SpecFor(handle->app_id).feature_schema).Predict(replay.vector); }
        catch (const std::exception& failure) { throw RuntimeError(RuntimeCode::ModelError,failure.what()); }
        std::ostringstream json;
        json << std::setprecision(17) << "{\"schema\":\"harmony.ops.replay.v1\",\"status\":\"ok\",\"source\":\"offline_replay\",\"app_id\":"
            << JsonString(handle->app_id) << ",\"feature_schema\":" << JsonString(SpecFor(handle->app_id).feature_schema)
            << ",\"operation_label\":" << JsonString(prediction.operation_label) << ",\"cosine_similarity\":" << prediction.cosine_similarity
            << ",\"center_id\":" << prediction.center_id << ",\"page_size\":" << replay.page_size << ",\"accuracy_evaluated\":false}";
        Copy(json.str(),buffer,capacity,required);
    });
}
void hop_destroy(hop_handle* handle) { try { delete handle; } catch (...) {} }
const char* hop_status_name(hop_status status) { return RuntimeCodeName(static_cast<RuntimeCode>(status)); }
} // extern C
