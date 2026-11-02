#include "harmony_ops/runtime.hpp"
#include "harmony_ops/platform_bridge.h"

#include <algorithm>
#include <atomic>
#include <cerrno>
#include <csignal>
#include <cstring>
#include <fstream>
#include <iostream>
#include <limits>
#include <map>
#include <set>
#include <sstream>
#include <thread>

#if !defined(_WIN32)
#include <poll.h>
#include <unistd.h>
#endif

#if defined(__linux__) && !defined(__OHOS__)
#include <cerrno>
#include <fcntl.h>
#include <spawn.h>
#include <sys/wait.h>
#include <unistd.h>
extern char** environ;
#endif

namespace {
using namespace harmony_ops;
namespace fs = std::filesystem;
volatile std::sig_atomic_t interrupted = 0;
void Interrupt(int) { interrupted = 1; }
std::string Trim(std::string text) {
    const auto first = text.find_first_not_of(" \t\r\n");
    return first == std::string::npos ? "" : text.substr(first, text.find_last_not_of(" \t\r\n") - first + 1);
}
std::uint32_t Number(const std::string& value) {
    if (value.empty() || value.find_first_not_of("0123456789") != std::string::npos) throw std::invalid_argument("configuration integer required");
    const auto number = std::stoull(value);
    if (number > std::numeric_limits<std::uint32_t>::max()) throw std::invalid_argument("configuration integer overflow");
    return static_cast<std::uint32_t>(number);
}
#if defined(__linux__) && !defined(__OHOS__)
// argv-only child, one second deadline; never execute a shell or capture UI text.
std::string Xdotool(const std::vector<std::string>& arguments) {
    int pipe_fd[2];
    if (pipe2(pipe_fd, O_CLOEXEC)) throw std::runtime_error("focus pipe failed");
    const int flags = fcntl(pipe_fd[0], F_GETFL, 0);
    if (flags < 0 || fcntl(pipe_fd[0], F_SETFL, flags | O_NONBLOCK) < 0) {
        close(pipe_fd[0]); close(pipe_fd[1]); throw std::runtime_error("focus nonblocking pipe setup failed");
    }
    std::vector<char*> argv{const_cast<char*>("xdotool")};
    for (const auto& argument : arguments) argv.push_back(const_cast<char*>(argument.c_str()));
    argv.push_back(nullptr);
    posix_spawn_file_actions_t actions;
    pid_t child = -1;
    int spawn_error = posix_spawn_file_actions_init(&actions);
    if (!spawn_error) {
        spawn_error = posix_spawn_file_actions_adddup2(&actions, pipe_fd[1], STDOUT_FILENO);
        if (!spawn_error) spawn_error = posix_spawn_file_actions_addopen(&actions, STDERR_FILENO, "/dev/null", O_WRONLY, 0);
        if (!spawn_error) spawn_error = posix_spawn_file_actions_addclose(&actions, pipe_fd[0]);
        if (!spawn_error) spawn_error = posix_spawn_file_actions_addclose(&actions, pipe_fd[1]);
        if (!spawn_error) spawn_error = posix_spawnp(&child, "xdotool", &actions, nullptr, argv.data(), environ);
        posix_spawn_file_actions_destroy(&actions);
        close(pipe_fd[1]);
        if (spawn_error) { close(pipe_fd[0]); throw RuntimeError(RuntimeCode::FocusChanged, "xdotool is unavailable for this desktop account"); }
    } else { close(pipe_fd[0]); close(pipe_fd[1]); throw std::runtime_error("focus spawn setup failed"); }
    // The PID is the exact helper created here; only this process may be terminated.
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(1);
    std::string result;
    int status = 0;
    bool finished = false, overflow = false;
    while (std::chrono::steady_clock::now() < deadline) {
        char buffer[128];
        const auto count = read(pipe_fd[0], buffer, sizeof(buffer));
        if (count > 0) result.append(buffer, static_cast<std::size_t>(count));
        if (result.size() > 256) { overflow = true; break; }
        const auto waited = waitpid(child, &status, WNOHANG);
        if (waited == child) {
            while (true) {
                const auto last = read(pipe_fd[0], buffer, sizeof(buffer));
                if (last <= 0) break;
                result.append(buffer, static_cast<std::size_t>(last));
                if (result.size() > 256) { overflow = true; break; }
            }
            finished = true; break;
        }
        if (waited < 0 && errno != EINTR) break;
        std::this_thread::sleep_for(std::chrono::milliseconds(5));
    }
    close(pipe_fd[0]);
    if (!finished) { kill(child, SIGKILL); while (waitpid(child, &status, 0) < 0 && errno == EINTR) {} }
    if (!finished || overflow || !WIFEXITED(status) || WEXITSTATUS(status) != 0)
        throw RuntimeError(RuntimeCode::FocusChanged, "xdotool focus unavailable/timeout; use the desktop account and verified X11 provider");
    result = Trim(result);
    (void)Number(result);
    return result;
}
std::int32_t ParentPid(std::int32_t pid) {
    std::ifstream input("/proc/" + std::to_string(pid) + "/stat");
    std::string text; std::getline(input, text);
    const auto end = text.rfind(") ");
    if (!input || end == std::string::npos) throw RuntimeError(RuntimeCode::ProcessChanged, "cannot verify child ownership");
    std::istringstream fields(text.substr(end + 2));
    char state; std::int32_t parent = 0;
    if (!(fields >> state >> parent) || state == 'Z' || state == 'X') throw RuntimeError(RuntimeCode::ProcessChanged, "invalid child ownership");
    return parent;
}
std::set<std::int32_t> Descendants(std::int32_t root) {
    std::map<std::int32_t,std::int32_t> parents;
    for (const auto& entry : fs::directory_iterator("/proc")) {
        const auto name = entry.path().filename().string();
        if (name.empty() || name.find_first_not_of("0123456789") != std::string::npos) continue;
        const auto pid = Number(name);
        if (pid > 2147483647) continue;
        try { parents[static_cast<std::int32_t>(pid)] = ParentPid(static_cast<std::int32_t>(pid)); }
        catch (const RuntimeError&) { /* Unrelated process may exit/be hidden during enumeration. Selected PIDs are checked below. */ }
    }
    std::set<std::int32_t> result{root};
    bool changed = true;
    while (changed) {
        changed = false;
        for (const auto& [pid,parent] : parents)
            if (result.count(parent) && result.insert(pid).second) changed = true;
    }
    return result;
}
std::string AddedProcessMetadata(const std::vector<std::int32_t>& pids) {
    // Diagnosis only. Never sample/clear an unapproved PID, read cmdline,
    // collect document text, or treat an exited child as approved coverage.
    std::string out="[";
    for (auto pid:pids) {
        if(out.size()>1)out+=',';
        out+="{\"pid\":"+std::to_string(pid);
        try {
            std::ifstream input("/proc/"+std::to_string(pid)+"/stat");std::string text;std::getline(input,text);
            const auto begin=text.find('('),end=text.rfind(") ");
            if(!input || begin==std::string::npos || end==std::string::npos || end<=begin)throw std::runtime_error("stat unavailable");
            std::istringstream fields(text.substr(end+2));std::string token,state,parent,ticks;
            for(unsigned field=3;field<=22;++field){
                if(!(fields>>token))throw std::runtime_error("stat incomplete");
                if(field==3)state=token;else if(field==4)parent=token;else if(field==22)ticks=token;
            }
            if(parent.empty() || ticks.empty() || parent.find_first_not_of("0123456789")!=std::string::npos || ticks.find_first_not_of("0123456789")!=std::string::npos)
                throw std::runtime_error("stat invalid");
            out+=",\"metadata_status\":\"observed\",\"comm\":"+JsonString(text.substr(begin+1,end-begin-1))+
                ",\"state\":"+JsonString(state)+",\"ppid\":"+parent+",\"start_ticks\":"+ticks;
        } catch(...) {out+=",\"metadata_status\":\"unavailable_or_exited\"";}
        out+='}';
    }
    return out+"]";
}
#endif
class DesktopBridge final : public PlatformBridge {
public:
    FocusEvidence selected;
    fs::path raw_output;
    std::string raw_phase;
    std::size_t raw_index=0;
    FeatureMap Sample(const hc_process_identity& process,const std::string& schema) override {
        if (raw_output.empty()) return PlatformBridge::Sample(process,schema);
        hc_snapshot snapshot{}; hc_error error{};
        const auto mode=schema==SpecFor("wps").feature_schema?HC_SEGMENT_WPS:HC_SEGMENT_CROSS_APP;
        if (hc_collect(&process,mode,&snapshot,&error)!=HC_OK)
            throw RuntimeError(error.code==HC_PROCESS_CHANGED?RuntimeCode::ProcessChanged:RuntimeCode::CollectionFailed,error.message);
        try {
            auto map=SemanticFeatures(snapshot,schema);
            sampled_page_size_=snapshot.page_size;
            std::ofstream out(raw_output,std::ios::binary|std::ios::app);
            out << "{\"schema\":\"harmony.ops.raw-window.v1\",\"phase\":" << JsonString(raw_phase)
                << ",\"index\":" << raw_index << ",\"feature_schema\":" << JsonString(schema)
                << ",\"pid\":" << process.pid << ",\"start_ticks\":" << process.start_ticks
                << ",\"page_size\":" << snapshot.page_size << ",\"started_ns\":" << snapshot.started_ns
                << ",\"finished_ns\":" << snapshot.finished_ns << ",\"vmas\":[";
            for (std::size_t i=0;i<snapshot.count;++i) {
                const auto& v=snapshot.vmas[i]; if (i) out << ',';
                out << "{\"start\":" << v.start << ",\"end\":" << v.end << ",\"offset\":" << v.offset
                    << ",\"inode\":" << v.inode << ",\"device\":" << JsonString(v.device)
                    << ",\"pathname\":" << JsonString(v.pathname) << ",\"segment\":" << JsonString(v.segment)
                    << ",\"perms\":" << JsonString(v.permissions) << ",\"referenced_bytes\":" << v.referenced_bytes
                    << ",\"referenced_pages\":" << v.referenced_pages << '}';
            }
            out << "]}\n"; out.flush();
            if (!out) throw RuntimeError(RuntimeCode::OutputFailed,"raw snapshot output failed");
            out.close(); if (!out) throw RuntimeError(RuntimeCode::OutputFailed,"raw snapshot close failed");
            hc_snapshot_free(&snapshot); return map;
        } catch (...) { hc_snapshot_free(&snapshot); throw; }
    }
    FocusEvidence Focus() override {
        hop_focus host{};
        const int result = ReadOperationFocus(nullptr,&host);
        if (result == 0) {
            if (!std::memchr(host.app_id,0,sizeof(host.app_id)) || !std::memchr(host.window_id,0,sizeof(host.window_id)) ||
                !std::memchr(host.evidence_source,0,sizeof(host.evidence_source)) || !host.pid_count || host.pid_count > HOP_MAX_PIDS)
                throw RuntimeError(RuntimeCode::FocusChanged,"invalid target focus evidence");
            FocusEvidence evidence{host.app_id,host.window_id,host.evidence_source,host.root_pid,
                std::vector<std::int32_t>(host.covered_pids,host.covered_pids+host.pid_count)};
            if (evidence.root_pid != selected.root_pid ||
                std::set<std::int32_t>(evidence.covered_pids.begin(),evidence.covered_pids.end()) != std::set<std::int32_t>(selected.covered_pids.begin(),selected.covered_pids.end()))
                throw RuntimeError(RuntimeCode::ProcessChanged,"target focus process coverage differs from authorized configuration");
            return evidence;
        }
        if (result != -1) throw RuntimeError(RuntimeCode::FocusChanged,"target focus provider failed");
#if defined(__linux__) && !defined(__OHOS__)
        const auto window = Xdotool({"getactivewindow"});
        const auto pid = Number(Xdotool({"getwindowpid", window}));
        if (window != Xdotool({"getactivewindow"}) || std::find(selected.covered_pids.begin(), selected.covered_pids.end(), static_cast<std::int32_t>(pid)) == selected.covered_pids.end())
            throw RuntimeError(RuntimeCode::FocusChanged, "focus moved outside the selected application");
        for (const auto covered : selected.covered_pids) {
            auto ancestor = covered;
            for (unsigned depth=0; ancestor != selected.root_pid && ancestor > 1 && depth < 64; ++depth) ancestor = ParentPid(ancestor);
            if (ancestor != selected.root_pid) throw RuntimeError(RuntimeCode::ProcessChanged, "selected PID is not a child of root_pid");
        }
        const auto actual=Descendants(selected.root_pid);
        const std::set<std::int32_t> expected(selected.covered_pids.begin(),selected.covered_pids.end());
        if (actual != expected) {
            const auto list=[](const auto& ids) { std::string text="[";for(auto id:ids){if(text.size()>1)text+=',';text+=std::to_string(id);}return text+"]"; };
            std::vector<std::int32_t> added,removed;
            std::set_difference(actual.begin(),actual.end(),expected.begin(),expected.end(),std::back_inserter(added));
            std::set_difference(expected.begin(),expected.end(),actual.begin(),actual.end(),std::back_inserter(removed));
            throw RuntimeError(RuntimeCode::ProcessChanged, "application child coverage changed; expected="+list(expected)+
                "; actual="+list(actual)+"; added="+list(added)+"; removed="+list(removed)+"; monotonic_ns="+std::to_string(MonotonicNowNs())+
                "; update covered_pids and begin a complete new baseline; added_stat="+AddedProcessMetadata(added));
        }
        auto evidence = selected;
        evidence.window_id = window;
        evidence.evidence_source = "x11.xdotool.active-window-pid";
        return evidence;
#else
        throw RuntimeError(RuntimeCode::FocusChanged, "this executable has no target focus API; link platform_bridge.cpp into the host or implement its closed focus call");
#endif
    }
    void Stage(const std::string& phase, std::size_t index, std::uint64_t ns) override {
        raw_phase=phase; raw_index=index;
        std::cout << "{\"schema\":\"harmony.ops.stage.v1\",\"phase\":" << JsonString(phase)
                  << ",\"index\":" << index << ",\"monotonic_ns\":" << ns << "}\n" << std::flush;
        if (!std::cout) throw RuntimeError(RuntimeCode::OutputFailed, "phase output failed");
    }
};

bool NextTrigger(std::string& buffered, std::string& line) {
    while (!interrupted) {
        const auto end = buffered.find('\n');
        if (end != std::string::npos) {
            if (end > 4096) throw std::invalid_argument("controlled trigger exceeds 4096 bytes");
            line = Trim(buffered.substr(0,end)); buffered.erase(0,end+1); return true;
        }
        if (buffered.size() > 4096) throw std::invalid_argument("controlled trigger exceeds 4096 bytes");
#if !defined(_WIN32)
        pollfd input{STDIN_FILENO,POLLIN,0};
        const auto ready = poll(&input,1,100);
        if (ready < 0) { if (errno == EINTR) continue; throw std::runtime_error("trigger input poll failed"); }
        if (!ready) continue;
        if (!(input.revents & (POLLIN|POLLHUP))) throw std::runtime_error("trigger input failed");
        char bytes[512];
        const auto count = read(STDIN_FILENO,bytes,sizeof(bytes));
        if (count < 0) { if (errno == EINTR) continue; throw std::runtime_error("trigger input read failed"); }
        if (!count) {
            if (!buffered.empty()) throw std::invalid_argument("truncated controlled trigger line");
            return false;
        }
        buffered.append(bytes,static_cast<std::size_t>(count));
#else
        return static_cast<bool>(std::getline(std::cin,line));
#endif
    }
    return false;
}
} // namespace

int RunPlatformControlled(const std::filesystem::path& file, const std::string& window,
                          const std::string& event, const std::filesystem::path& output, bool persistent) {
    using namespace harmony_ops;
    std::ifstream input(file);
    if (!input) throw std::runtime_error("cannot open device configuration");
    std::map<std::string,std::string> config;
    std::string line;
    const std::set<std::string> allowed{"app_id","models_root","model_profile","session_id","root_pid","covered_pids","feature_scope","output_jsonl","raw_output_jsonl","baseline_ms","baseline_count","action_ms","post_ms","max_sample_ms","focus_poll_ms","history_limit","dedup_limit"};
    while (std::getline(input,line)) {
        line = Trim(line);
        if (line.empty() || line[0] == '#') continue;
        const auto at = line.find('=');
        if (at == std::string::npos) throw std::invalid_argument("configuration requires key=value");
        const auto key = Trim(line.substr(0,at)), value = Trim(line.substr(at+1));
        if (!allowed.count(key) || value.empty() || !config.emplace(key,value).second) throw std::invalid_argument("unknown/duplicate/empty configuration key");
    }
    if (input.bad()) throw std::runtime_error("configuration read failed");
    const auto required = [&](const std::string& key) { if (!config.count(key)) throw std::invalid_argument("missing configuration key: " + key); return config.at(key); };
    RuntimeConfig runtime;
    runtime.app_id = required("app_id");
    runtime.models_root = file.parent_path() / required("models_root");
    if (config.count("model_profile")) runtime.model_profile=config.at("model_profile");
    if (config.count("feature_scope")) {
        if (config.at("feature_scope")!="root_only" && config.at("feature_scope")!="all_covered")
            throw std::invalid_argument("feature_scope must be root_only or all_covered");
        runtime.root_features_only=config.at("feature_scope")=="root_only";
    }
    runtime.session_id = required("session_id");
    runtime.output_jsonl = output.empty() ? file.parent_path() / required("output_jsonl") : output;
    if (fs::exists(runtime.output_jsonl)) throw std::invalid_argument("run output must be a new JSONL file");
    const auto set = [&](const char* key, auto& value) { if (config.count(key)) value = Number(config.at(key)); };
    set("baseline_ms",runtime.baseline_ms); set("baseline_count",runtime.baseline_count);
    set("action_ms",runtime.action_ms); set("post_ms",runtime.post_ms);
    set("max_sample_ms",runtime.max_sample_ms); set("focus_poll_ms",runtime.focus_poll_ms);
    set("history_limit",runtime.history_limit); set("dedup_limit",runtime.dedup_limit);
    DesktopBridge bridge;
    if (config.count("raw_output_jsonl")) {
        bridge.raw_output=file.parent_path()/config.at("raw_output_jsonl");
        if (bridge.raw_output==runtime.output_jsonl || fs::exists(bridge.raw_output) ||
            !fs::is_directory(bridge.raw_output.parent_path()))
            throw std::invalid_argument("raw output requires a new distinct file in an existing private directory");
    }
    bridge.selected.app_id = runtime.app_id;
    const auto root = Number(required("root_pid"));
    if (!root || root > 2147483647) throw std::invalid_argument("set a positive authorized root_pid");
    bridge.selected.root_pid = static_cast<std::int32_t>(root);
    std::istringstream selected(required("covered_pids"));
    while (std::getline(selected,line,',')) {
        const auto pid = Number(Trim(line));
        if (!pid || pid > 2147483647) throw std::invalid_argument("set positive covered_pids");
        bridge.selected.covered_pids.push_back(static_cast<std::int32_t>(pid));
    }
    ServiceRuntime service(runtime,bridge);
    service.Start();
    interrupted = 0;
    const auto old_int = std::signal(SIGINT,Interrupt), old_term = std::signal(SIGTERM,Interrupt);
    std::atomic<bool> finished{false};
    std::thread monitor([&] {
        while (!finished.load()) {
            if (interrupted) { service.RequestStop(); return; }
            std::this_thread::sleep_for(std::chrono::milliseconds(20));
        }
    });
    const auto cleanup = [&] { finished.store(true); monitor.join(); service.Stop(); std::signal(SIGINT,old_int); std::signal(SIGTERM,old_term); };
    try {
        bool had_error = false;
        const auto run = [&](const std::string& window_id,const std::string& event_id) {
            const auto result = service.RunControlled(window_id,event_id);
            std::cout << EventJson(result) << '\n' << HealthJson(service.Health()) << '\n' << std::flush;
            if (!std::cout) throw RuntimeError(RuntimeCode::OutputFailed,"result output failed");
        };
        if (!persistent) run(window,event);
        else {
            std::string buffered,trigger;
            while (NextTrigger(buffered,trigger)) {
                if (trigger == "STOP") break;
                if (trigger.empty()) continue;
                const auto tab = trigger.find('\t');
                if (tab == std::string::npos || tab == 0 || tab+1 == trigger.size() || trigger.find('\t',tab+1) != std::string::npos)
                    throw std::invalid_argument("trigger requires window_id<TAB>event_id");
                try { run(trigger.substr(0,tab),trigger.substr(tab+1)); }
                catch (const RuntimeError& error) {
                    had_error = true;
                    std::cerr << "{\"schema\":\"harmony.ops.error.v1\",\"status\":\"error\",\"code\":" << JsonString(RuntimeCodeName(error.code()))
                              << ",\"message\":" << JsonString(error.what()) << "}\n";
                    if (error.code() == RuntimeCode::Cancelled) break;
                }
            }
            std::cout << "{\"schema\":\"harmony.ops.history.v1\",\"events\":[";
            bool first = true;
            for (const auto& recognized : service.History()) { if (!first) std::cout << ','; first = false; std::cout << EventJson(recognized); }
            std::cout << "]}\n" << HealthJson(service.Health()) << '\n' << std::flush;
            if (!std::cout) throw RuntimeError(RuntimeCode::OutputFailed,"history output failed");
        }
        cleanup();
        return had_error || interrupted ? 2 : 0;
    } catch (...) { cleanup(); throw; }
}
