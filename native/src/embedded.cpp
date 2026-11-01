#include "harmony_ops/embedded.h"
#include "harmony_ops/platform_bridge.h"
#include "harmony_ops/replay.hpp"
#include "harmony_ops/runtime.hpp"

#include <algorithm>
#include <atomic>
#include <cmath>
#include <condition_variable>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <memory>
#include <mutex>
#include <sstream>
#include <thread>
#if defined(_WIN32)
#define NOMINMAX
#include <windows.h>
#endif

namespace fs=std::filesystem;
using namespace harmony_ops;
namespace {
hop_status SetError(hop_error* error,hop_status code,const char* message) noexcept {
    if (error) { error->code=code; std::snprintf(error->message,sizeof(error->message),"%s",message?message:""); }
    return code;
}
void Checked(hop_status code,const hop_error& error) {
    if (code!=HOP_OK) throw RuntimeError(static_cast<RuntimeCode>(code),error.message);
}
void Write(const fs::path& file,const std::string& content,bool append=false) {
    // Status replacement is atomic on a single filesystem; results remain
    // owned by the common runtime. No fsync/durable-storage claim is made.
    const auto destination=append?file:fs::path(file.string()+".tmp");
    std::ofstream out(destination,std::ios::binary|(append?std::ios::app:std::ios::trunc));
    if (!out || !(out << content) || !out.flush()) throw RuntimeError(RuntimeCode::OutputFailed,"cannot write onsite file: "+file.filename().string());
    out.close();
    if (!out) throw RuntimeError(RuntimeCode::OutputFailed,"cannot close onsite report");
    if (!append) {
#if defined(_WIN32)
        if (!MoveFileExW(destination.c_str(),file.c_str(),MOVEFILE_REPLACE_EXISTING))
            throw RuntimeError(RuntimeCode::OutputFailed,"cannot replace onsite report");
#else
        fs::rename(destination,file);
#endif
    }
}
const char* Treatment(hop_status code) {
    switch(code) {
    case HOP_OK:return "离线自检通过后，触发一次受控窗口；action 阶段执行动作，结果见 results.jsonl。";
    case HOP_FOCUS_CHANGED:return "核对 platform_bridge.cpp 的真实前台应用/窗口/主进程/完整 PID 集合；不要填固定焦点。";
    case HOP_PROCESS_CHANGED:return "目标进程已退出、重启或身份读取失败；核对真实 PID 和启动身份，再触发完整窗口。";
    case HOP_COLLECTION_FAILED:return "核对实际服务身份的 smaps/Referenced/clear_refs 权限和字段；不要提权绕过或生成零向量。";
    case HOP_MODEL_ERROR:return "核对完整 models 目录、应用 ID 和原文件 SHA；不要修改参数或跳过校验。";
    case HOP_OUTPUT_FAILED:return "核对输出目录可写、剩余空间和发布回调；诊断失败时停止继续触发。";
    case HOP_SAMPLING_TIMEOUT:return "真实平台回调或采样超时；检查接口是否阻塞，保持原窗口协议。";
    case HOP_CANCELLED:return "窗口已取消，不计有效识别；服务退出时属于预期行为。";
    default:return "查看 status.json 的 code/message 和 stage.jsonl；修正本机接口或配置，不将失败记为成功。";
    }
}
void Golden(const fs::path& package,const std::string& app) {
    const auto model=CentroidModel::Load(package/"models",app,SpecFor(app).feature_schema);
    const auto replay=LoadRawReplay(package/"fixtures/raw-window/manifest.csv",app);
    const auto prediction=model.Predict(replay.vector);
    std::ifstream input(package/"fixtures/raw-window"/(app+"_expected.csv"));
    std::vector<std::string> row;
    if (!ReadCsvRow(input,row) || row.size()!=2050 || !ReadCsvRow(input,row) || row.size()!=2050)
        throw RuntimeError(RuntimeCode::ModelError,"missing/invalid synthetic golden fixture");
    double delta=0;
    for (std::size_t i=0;i<kVectorDimension;++i)
        delta=std::max(delta,std::abs(replay.vector[i]-ParseFiniteDouble(row[i+2],"golden vector")));
    if (row[0]!=prediction.operation_label || std::abs(prediction.cosine_similarity-ParseFiniteDouble(row[1],"golden score"))>1e-9 || delta>1e-9)
        throw RuntimeError(RuntimeCode::ModelError,"raw replay differs from frozen golden");
}
std::string Snapshot(hop_handle* core) {
    hop_error error{};std::size_t size=0;
    Checked(hop_health_json(core,nullptr,0,&size,&error),error);
    std::vector<char> buffer(size);
    Checked(hop_health_json(core,buffer.data(),buffer.size(),&size,&error),error);
    return buffer.data();
}
} // namespace

struct hop_embedded {
    hop_handle* core=nullptr;
    hop_callbacks platform{};
    fs::path run;
    std::string session,callback_error;
    std::mutex mutex;
    std::condition_variable condition;
    std::thread worker;
    bool stopping=false,busy=false,pending=false,diagnostics_failed=false,offline_ready=false;
    std::size_t sequence=0;
    ~hop_embedded() { if (core) hop_destroy(core); }

    static int Focus(void* context,hop_focus* out) {
        auto& self=*static_cast<hop_embedded*>(context);
        try {
            const auto code=self.platform.focus?self.platform.focus(self.platform.context,out):-1;
            if (code) self.callback_error=code==-1?"平台焦点接口未接入（默认返回 -1）。":"真实焦点接口返回失败。";
            return code;
        } catch (const std::exception& failure) { self.callback_error=failure.what();return 1; }
          catch (...) { self.callback_error="焦点回调抛出未知异常。";return 1; }
    }
    static int Stage(void* context,const char* phase,std::size_t index,std::uint64_t ns) {
        auto& self=*static_cast<hop_embedded*>(context);
        try {
            Write(self.run/"stage.jsonl","{\"schema\":\"harmony.ops.stage.v1\",\"phase\":"+JsonString(phase)+
                  ",\"index\":"+std::to_string(index)+",\"monotonic_ns\":"+std::to_string(ns)+"}\n",true);
            const auto code=self.platform.stage?self.platform.stage(self.platform.context,phase,index,ns):0;
            if (code) self.callback_error="平台阶段回调返回失败。";
            return code;
        } catch (const std::exception& failure) { self.callback_error=failure.what();return 1; }
          catch (...) { self.callback_error="阶段记录失败。";return 1; }
    }
    static int Publish(void* context,const char* json,std::size_t length) {
        auto& self=*static_cast<hop_embedded*>(context);
        // One authoritative channel. Do not claim an atomic transaction across
        // both a host IPC channel and a file. Default needs no host callback.
        try {
            if (self.platform.publish) {
                const auto code=self.platform.publish(self.platform.context,json,length);
                if (code) self.callback_error="宿主发布回调返回失败。";
                return code;
            }
            Write(self.run/"results.jsonl",std::string(json,length)+"\n",true);
            return 0;
        } catch (const std::exception& failure) { self.callback_error=failure.what();return 1; }
          catch (...) { self.callback_error="结果输出失败。";return 1; }
    }
    static int Identity(void* context,int32_t pid,uint64_t* ticks) {
        auto& self=*static_cast<hop_embedded*>(context);
        return self.platform.identity(self.platform.context,pid,ticks);
    }
    static int Clear(void* context,int32_t pid,uint64_t ticks) {
        auto& self=*static_cast<hop_embedded*>(context);
        return self.platform.clear_refs(self.platform.context,pid,ticks);
    }
    static int Smaps(void* context,int32_t pid,uint64_t ticks,char* buffer,std::size_t capacity,std::size_t* length,uint64_t* page_size) {
        auto& self=*static_cast<hop_embedded*>(context);
        return self.platform.read_smaps(self.platform.context,pid,ticks,buffer,capacity,length,page_size);
    }
    void Report(const char* state,hop_status code,const char* message) {
        const auto health=core?Snapshot(core):"null";
        const std::string json="{\"schema\":\"harmony.ops.embedded-status.v1\",\"state\":"+JsonString(state)+
            ",\"code\":"+JsonString(hop_status_name(code))+",\"message\":"+JsonString(message)+
            ",\"offline_ready\":"+(offline_ready?"true":"false")+",\"run_directory\":"+JsonString(run.string())+
            ",\"controlled_window_only\":true,\"autonomous_segmentation\":false,\"health\":"+health+"}\n";
        Write(run/"status.json",json);
        Write(run/"status.txt",std::string("状态：")+state+"\n错误码："+hop_status_name(code)+"\n说明："+message+
              "\n处理："+Treatment(code)+"\n目录："+run.string()+"\n启动成功仅代表离线就绪，真实采集以窗口结果为准。\n");
        Write(run/"status-history.jsonl",json,true);
    }
    void Work() noexcept {
      try {
        for (;;) {
            std::string id;
            {
                std::unique_lock<std::mutex> lock(mutex);
                condition.wait(lock,[&]{return stopping||pending;});
                if (stopping) return;
                pending=false;id=session+"-"+std::to_string(sequence);
            }
            hop_error error{};
            callback_error.clear();
            const auto code=hop_run(core,id.c_str(),id.c_str(),&error);
            const std::string message=std::string(error.message)+(callback_error.empty()?"":"; "+callback_error);
            try { Report(code==HOP_OK?"window_success":"window_failed",code,message.c_str()); }
            catch (...) {
                // A result may already have been committed by the core.
                // Do not retroactively claim rollback or allow another job.
                std::lock_guard<std::mutex> lock(mutex);diagnostics_failed=true;
            }
            { std::lock_guard<std::mutex> lock(mutex);busy=false; }
        }
      } catch (...) {
        std::lock_guard<std::mutex> lock(mutex);diagnostics_failed=true;busy=false;
      }
    }
};

extern "C" hop_status hop_embedded_start(const char* package_root,const char* output_root,const char* app_id,
                                         hop_embedded** out,hop_error* error) {
    if (!out) return SetError(error,HOP_INVALID_ARGUMENT,"out handle is required");
    if (*out) return SetError(error,HOP_BUSY,"stop the existing instance before starting again");
    std::unique_ptr<hop_embedded> service;
    try {
        if (!out || !package_root || !*package_root || !output_root || !*output_root || !app_id || !*app_id)
            throw std::invalid_argument("package/output/app and out handle are required");
        (void)SpecFor(app_id);
        const auto package=fs::absolute(package_root);
        service=std::make_unique<hop_embedded>();
        static std::atomic<std::uint64_t> serial{0};
        service->session="onsite-"+std::to_string(MonotonicNowNs())+"-"+std::to_string(++serial);
        const auto output=fs::absolute(output_root);fs::create_directories(output);
        const auto run=output/service->session;
        if (!fs::create_directory(run)) throw RuntimeError(RuntimeCode::OutputFailed,"unique run directory already exists");
        service->run=run;
        service->Report("selftest_running",HOP_OK,"自动检查模型 SHA 和合成原始窗口，无真实进程采集。");
        try { Golden(package,app_id); }
        catch (const RuntimeError&) { throw; }
        catch (const std::exception& failure) { throw RuntimeError(RuntimeCode::ModelError,failure.what()); }
        hop_config config;hop_default_config(&config);
        const auto models=(package/"models").string(),result=(service->run/"results.jsonl").string();
        config.models_root=models.c_str();config.app_id=app_id;config.session_id=service->session.c_str();config.output_jsonl=result.c_str();
        hop_default_callbacks(&service->platform);ConfigureOperationCallbacks(&service->platform);
        hop_callbacks callbacks=service->platform;callbacks.context=service.get();
        callbacks.focus=hop_embedded::Focus;callbacks.stage=hop_embedded::Stage;callbacks.publish=hop_embedded::Publish;
        if (callbacks.identity) callbacks.identity=hop_embedded::Identity;
        if (callbacks.clear_refs) callbacks.clear_refs=hop_embedded::Clear;
        if (callbacks.read_smaps) callbacks.read_smaps=hop_embedded::Smaps;
        hop_error inner{};
        Checked(hop_create(&config,&callbacks,&service->core,&inner),inner);
        Checked(hop_start(service->core,&inner),inner);
        service->offline_ready=true;
        service->Report("offline_ready_live_pending",HOP_OK,"模型与回放通过，等待无标签触发；尚未验证真实焦点/采集权限。");
        service->worker=std::thread([instance=service.get()]{instance->Work();});
        *out=service.release();return SetError(error,HOP_OK,"");
    } catch (const RuntimeError& failure) {
        const auto code=static_cast<hop_status>(failure.code());
        if (service && !service->run.empty()) try { service->Report("startup_failed",code,failure.what()); } catch (...) {}
        return SetError(error,code,failure.what());
    } catch (const std::invalid_argument& failure) {
        if (service && !service->run.empty()) try { service->Report("startup_failed",HOP_INVALID_ARGUMENT,failure.what()); } catch (...) {}
        return SetError(error,HOP_INVALID_ARGUMENT,failure.what());
    } catch (const fs::filesystem_error& failure) {
        if (service && !service->run.empty()) try { service->Report("startup_failed",HOP_OUTPUT_FAILED,failure.what()); } catch (...) {}
        return SetError(error,HOP_OUTPUT_FAILED,failure.what());
    } catch (const std::exception& failure) {
        if (service && !service->run.empty()) try { service->Report("startup_failed",HOP_INTERNAL_ERROR,failure.what()); } catch (...) {}
        return SetError(error,HOP_INTERNAL_ERROR,failure.what());
    } catch (...) { return SetError(error,HOP_INTERNAL_ERROR,"unknown embedded startup failure"); }
}
extern "C" hop_status hop_embedded_trigger(hop_embedded* service,hop_error* error) {
    try {
        if (!service) return SetError(error,HOP_NOT_STARTED,"service is not started");
        std::lock_guard<std::mutex> lock(service->mutex);
        if (service->stopping) return SetError(error,HOP_NOT_STARTED,"service is stopping");
        if (service->diagnostics_failed) return SetError(error,HOP_OUTPUT_FAILED,"onsite report failed; restart after checking output directory");
        if (service->busy) return SetError(error,HOP_BUSY,"one controlled window is already pending/running");
        service->busy=true;service->pending=true;++service->sequence;service->condition.notify_one();
        return SetError(error,HOP_OK,"accepted; result is asynchronous and appears in status/results");
    } catch (...) { return SetError(error,HOP_INTERNAL_ERROR,"embedded trigger failure"); }
}
extern "C" hop_status hop_embedded_stop(hop_embedded** pointer,hop_error* error) {
    if (!pointer || !*pointer) return SetError(error,HOP_OK,"");
    auto* service=*pointer;
    try {
        if (service->worker.get_id()==std::this_thread::get_id())
            return SetError(error,HOP_BUSY,"stop must run on the host control thread, outside callbacks");
        { std::lock_guard<std::mutex> lock(service->mutex);service->stopping=true; }
        hop_error inner{};hop_request_stop(service->core,&inner);service->condition.notify_one();
        if (service->worker.joinable()) service->worker.join();
        Checked(hop_stop(service->core,&inner),inner);
        hop_status status=HOP_OK;
        try { service->Report("stopped",HOP_OK,"已取消采样并停止工作线程；取消窗口不计有效识别。"); }
        catch (...) { status=HOP_OUTPUT_FAILED; }
        delete service;*pointer=nullptr;
        return SetError(error,status,status==HOP_OK?"":"stopped, but final onsite report could not be written");
    } catch (const std::exception& failure) { return SetError(error,HOP_INTERNAL_ERROR,failure.what()); }
      catch (...) { return SetError(error,HOP_INTERNAL_ERROR,"embedded shutdown failure"); }
}
