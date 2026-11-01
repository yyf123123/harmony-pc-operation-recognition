// Synthetic callback/owned worker contracts. Never real WPS or accuracy data.
#include "harmony_ops/embedded.h"
#include "harmony_ops/runtime.hpp"
#include <atomic>
#include <chrono>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <sstream>
#include <thread>

namespace fs=std::filesystem;
namespace {
struct Fixture { std::atomic<bool> fail_focus{false},action{false},entered{false}; } fixture;
void Require(bool value,const char* message) { if (!value) throw std::runtime_error(message); }
std::string Read(const fs::path& file) { std::ifstream in(file);std::ostringstream text;text<<in.rdbuf();return text.str(); }
fs::path Run(const fs::path& root) {
    fs::path result;
    for (const auto& p:fs::directory_iterator(root)) if (p.is_directory()) { Require(result.empty(),"unique test run");result=p.path(); }
    Require(!result.empty(),"run directory missing");return result;
}
template<class F> void Wait(F predicate) {
    const auto end=std::chrono::steady_clock::now()+std::chrono::seconds(40);
    while (!predicate()) { if (std::chrono::steady_clock::now()>end) throw std::runtime_error("worker timed out");std::this_thread::sleep_for(std::chrono::milliseconds(10)); }
}
int Focus(void*,hop_focus* out) {
    if (fixture.fail_focus) return 1;
    std::strcpy(out->app_id,"wps");std::strcpy(out->window_id,"synthetic-window");std::strcpy(out->evidence_source,"synthetic-embedded-test");
    out->root_pid=123;out->covered_pids[0]=123;out->pid_count=1;return 0;
}
int Stage(void*,const char* phase,size_t,uint64_t) {
    fixture.entered=true;fixture.action=std::strcmp(phase,"baseline")!=0;return 0;
}
int Identity(void*,int32_t,uint64_t* ticks) { *ticks=456;return 0; }
int Clear(void*,int32_t,uint64_t) { return 0; }
int Smaps(void*,int32_t,uint64_t,char* buffer,size_t capacity,size_t* length,uint64_t* page) {
    const char* raw=fixture.action?"1000-2000 rw-p 0 00:00 0 [heap]\nReferenced: 4 kB\n":
        "1000-2000 rw-p 0 00:00 0 [heap]\nReferenced: 0 kB\n";
    *length=std::strlen(raw);*page=4096;
    if (!buffer && !capacity) return 0;
    if (capacity<*length) return 1;
    std::memcpy(buffer,raw,*length);return 0;
}
}
extern "C" void ConfigureOperationCallbacks(hop_callbacks* callbacks) {
    callbacks->focus=Focus;callbacks->stage=Stage;callbacks->identity=Identity;callbacks->clear_refs=Clear;callbacks->read_smaps=Smaps;
}
int main(int argc,char** argv) {
    hop_embedded* service=nullptr;hop_error error{};
    const auto root=fs::temp_directory_path()/("ops-embedded-synthetic-"+std::to_string(harmony_ops::MonotonicNowNs()));
    try {
        Require(argc==3,"MODELS FIXTURES required");fs::create_directories(root/"package");
        fs::copy(argv[1],root/"package/models",fs::copy_options::recursive);
        fs::create_directories(root/"package/fixtures");fs::copy(argv[2],root/"package/fixtures/raw-window",fs::copy_options::recursive);
        const auto package=(root/"package").string();
        Require(hop_embedded_trigger(nullptr,&error)==HOP_NOT_STARTED,"null trigger");
        Require(hop_embedded_stop(&service,&error)==HOP_OK,"null stop idempotent");
        auto output=(root/"bad-app").string();
        Require(hop_embedded_start(package.c_str(),output.c_str(),"unknown",&service,&error)!=HOP_OK && !service,"unknown app must fail");
        {std::ofstream out(root/"output-is-file");out<<"synthetic";}
        output=(root/"output-is-file").string();
        Require(hop_embedded_start(package.c_str(),output.c_str(),"wps",&service,&error)==HOP_OUTPUT_FAILED && !service,"bad output cannot start");
        {std::ofstream out(root/"package/fixtures/raw-window/wps_expected.csv");out<<"invalid";}
        output=(root/"bad-golden").string();
        Require(hop_embedded_start(package.c_str(),output.c_str(),"wps",&service,&error)==HOP_MODEL_ERROR && !service,"golden startup must fail");
        Require(Read(Run(root/"bad-golden")/"status.json").find("startup_failed")!=std::string::npos,"startup failure retained");
        fs::copy_file(fs::path(argv[2])/"wps_expected.csv",root/"package/fixtures/raw-window/wps_expected.csv",fs::copy_options::overwrite_existing);
        fixture.fail_focus=true;output=(root/"focus-failed").string();
        Require(hop_embedded_start(package.c_str(),output.c_str(),"wps",&service,&error)==HOP_OK,"offline start");
        auto* existing=service;
        Require(hop_embedded_start(package.c_str(),output.c_str(),"wps",&service,&error)==HOP_BUSY && service==existing,"repeated start preserves live handle");
        auto run=Run(root/"focus-failed");
        Require(Read(run/"status.json").find("offline_ready_live_pending")!=std::string::npos,"offline is not live success");
        Require(hop_embedded_trigger(service,&error)==HOP_OK,"accept failure trigger");
        Wait([&]{return Read(run/"status.json").find("window_failed")!=std::string::npos;});
        Require(Read(run/"status.json").find("focus_changed")!=std::string::npos,"focus failure code");
        Require(Read(run/"results.jsonl").find("\"status\":\"success\"")==std::string::npos,"failed focus cannot predict");
        Require(hop_embedded_stop(&service,&error)==HOP_OK && !service,"failure stop");
        fixture.fail_focus=false;fixture.entered=false;output=(root/"cancelled").string();
        Require(hop_embedded_start(package.c_str(),output.c_str(),"wps",&service,&error)==HOP_OK,"cancel start");
        Require(hop_embedded_trigger(service,&error)==HOP_OK,"trigger nonblocking");
        Require(hop_embedded_trigger(service,&error)==HOP_BUSY,"reject overlap without queue");
        Wait([]{return fixture.entered.load();});
        const auto stop_start=std::chrono::steady_clock::now();
        Require(hop_embedded_stop(&service,&error)==HOP_OK && !service,"cancel and join");
        Require(std::chrono::steady_clock::now()-stop_start<std::chrono::seconds(2),"stop must cancel long window");
        run=Run(root/"cancelled");
        Require(Read(run/"results.jsonl").find("\"status\":\"success\"")==std::string::npos,"cancel cannot predict");
        output=(root/"stage-output-failed").string();
        Require(hop_embedded_start(package.c_str(),output.c_str(),"wps",&service,&error)==HOP_OK,"stage output start");
        run=Run(root/"stage-output-failed");fs::create_directory(run/"stage.jsonl");
        Require(hop_embedded_trigger(service,&error)==HOP_OK,"stage failure accepted");
        Wait([&]{return Read(run/"status.json").find("window_failed")!=std::string::npos;});
        Require(Read(run/"status.json").find("stage.jsonl")!=std::string::npos,"onsite report names failing stage file");
        Require(Read(run/"results.jsonl").find("\"status\":\"success\"")==std::string::npos,"stage failure cannot predict");
        Require(hop_embedded_stop(&service,&error)==HOP_OK,"stage failure stop");
        output=(root/"success").string();fixture.entered=false;
        Require(hop_embedded_start(package.c_str(),output.c_str(),"wps",&service,&error)==HOP_OK,"success start");
        Require(hop_embedded_trigger(service,&error)==HOP_OK,"success accepted");
        run=Run(root/"success");
        Wait([&]{return Read(run/"status.json").find("window_success")!=std::string::npos;});
        auto status=Read(run/"status.json");
        Require(status.find("last_success_timing_ns\":null")==std::string::npos && status.find("trigger_to_history_updated")!=std::string::npos,"post-output timing retained");
        Require(Read(run/"results.jsonl").find("\"status\":\"success\"")!=std::string::npos,"real common core result exists");
        Require(Read(run/"stage.jsonl").find("\"phase\":\"action\"")!=std::string::npos,"action prompt recorded");
        Require(hop_embedded_stop(&service,&error)==HOP_OK && !service,"normal stop");
        fs::remove_all(root);std::cout<<"embedded synthetic contracts passed; no real WPS/accuracy evaluation\n";return 0;
    } catch (const std::exception& failure) {
        hop_embedded_stop(&service,&error);fs::remove_all(root);std::cerr<<failure.what()<<'\n';return 1;
    }
}
