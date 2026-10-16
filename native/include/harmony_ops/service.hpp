#pragma once

#include "harmony_ops/service.h"

#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace harmony_ops {
// Optional RAII host wrapper. Only the stable C functions cross the library
// boundary; STL and exceptions here belong to the host translation unit.
class Service {
public:
    Service(const hop_config& config,const hop_callbacks& callbacks) {
        hop_error error{};
        Check(hop_create(&config,&callbacks,&handle_,&error),error);
    }
    ~Service() { hop_destroy(handle_); }
    Service(const Service&)=delete;
    Service& operator=(const Service&)=delete;
    Service(Service&& other) noexcept:handle_(std::exchange(other.handle_,nullptr)) {}
    Service& operator=(Service&& other) noexcept {
        if (this!=&other) { hop_destroy(handle_); handle_=std::exchange(other.handle_,nullptr); }
        return *this;
    }
    void Start() { hop_error error{}; Check(hop_start(handle_,&error),error); }
    void Run(const char* window,const char* event) { hop_error error{}; Check(hop_run(handle_,window,event,&error),error); }
    void Stop() { hop_error error{}; Check(hop_stop(handle_,&error),error); }
    void RequestStop() { hop_error error{}; Check(hop_request_stop(handle_,&error),error); }
    std::string HealthJson() const { return Snapshot(hop_health_json); }
    std::string HistoryJson() const { return Snapshot(hop_history_json); }
    std::string Replay(const char* manifest) const {
        hop_error error{}; size_t size=0;
        Check(hop_replay(handle_,manifest,nullptr,0,&size,&error),error);
        std::vector<char> buffer(size);
        Check(hop_replay(handle_,manifest,buffer.data(),buffer.size(),&size,&error),error);
        return buffer.data();
    }
    hop_handle* native_handle() noexcept { return handle_; }
private:
    static void Check(hop_status status,const hop_error& error) {
        if (status!=HOP_OK) throw std::runtime_error(std::string(hop_status_name(status))+": "+error.message);
    }
    using Getter=hop_status(*)(hop_handle*,char*,size_t,size_t*,hop_error*);
    std::string Snapshot(Getter getter) const {
        hop_error error{}; size_t size=0;
        Check(getter(handle_,nullptr,0,&size,&error),error);
        for (unsigned attempt=0;attempt<8;++attempt) {
            std::vector<char> buffer(size);
            const auto status=getter(handle_,buffer.data(),buffer.size(),&size,&error);
            if (status==HOP_OK) return buffer.data();
            if (status!=HOP_INVALID_ARGUMENT || size<=buffer.size()) Check(status,error);
        }
        throw std::runtime_error("runtime snapshot changed repeatedly");
    }
    hop_handle* handle_=nullptr;
};
} // namespace harmony_ops
