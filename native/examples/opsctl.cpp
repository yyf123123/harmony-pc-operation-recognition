#include "harmony_ops/collector.h"
#include "harmony_ops/runtime.hpp"
#include "harmony_ops/replay.hpp"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <map>
#include <set>
#include <sstream>
#include <stdexcept>

namespace fs = std::filesystem;
using namespace harmony_ops;

// Implemented beside the only platform-specific focus/host bridge.
int RunPlatformControlled(const fs::path&, const std::string&, const std::string&, const fs::path&, bool);

namespace {
std::string Json(const std::string& value) {
    std::ostringstream out;
    out << '"';
    for (unsigned char c : value) {
        if (c == '"' || c == '\\') out << '\\' << c;
        else if (c < 32) out << "\\u" << std::hex << std::setw(4) << std::setfill('0') << unsigned(c) << std::dec;
        else out << c;
    }
    return out.str() + '"';
}
std::string Read(const fs::path& file) {
    std::ifstream in(file, std::ios::binary);
    if (!in) throw std::runtime_error("cannot open input: " + file.string());
    std::ostringstream buffer;
    buffer << in.rdbuf();
    if (in.bad()) throw std::runtime_error("failed input read: " + file.string());
    return buffer.str();
}
void NewFile(const fs::path& path, const std::string& content) {
    if (path.empty() || fs::exists(path)) throw std::runtime_error("output must be a new file: " + path.string());
    if (!path.parent_path().empty()) fs::create_directories(path.parent_path());
#if defined(_WIN32)
    FILE* out = _wfopen(path.c_str(), L"wbx");
#else
    FILE* out = std::fopen(path.c_str(), "wbx");
#endif
    if (!out) throw std::runtime_error("cannot exclusively create output: " + path.string());
    const bool written = std::fwrite(content.data(), 1, content.size(), out) == content.size();
    const bool closed = std::fclose(out) == 0;
    if (!written || !closed) throw std::runtime_error("output failed: " + path.string());
}
std::uint64_t Unsigned(const std::string& text) {
    if (text.empty() || text.find_first_not_of("0123456789") != std::string::npos)
        throw std::invalid_argument("expected unsigned integer");
    std::size_t n = 0;
    const auto result = std::stoull(text, &n);
    if (n != text.size()) throw std::invalid_argument("invalid integer");
    return result;
}
std::string MapCsv(const FeatureMap& map) {
    std::ostringstream out;
    out << "namespace,feature_key,pages\n" << std::setprecision(17);
    for (const auto& [key, pages] : map) {
        WriteCsvField(out, key.first); out << ',';
        WriteCsvField(out, key.second); out << ',' << pages << '\n';
    }
    return out.str();
}
std::string VectorCsv(const std::vector<double>& vector) {
    std::ostringstream out;
    out << "sample_id";
    for (std::size_t i = 0; i < vector.size(); ++i) out << ",v" << std::setw(4) << std::setfill('0') << i;
    out << "\nraw-replay" << std::setprecision(17);
    for (double v : vector) out << ',' << v;
    return out.str() + '\n';
}
using Replay = ReplayWindow;
std::string PredictionJson(const std::string& app, const Prediction& prediction) {
    std::ostringstream out;
    out << std::setprecision(17) << "{\"schema\":\"harmony.ops.replay.v1\",\"status\":\"ok\",\"source\":\"offline_replay\",\"app_id\":" << Json(app)
        << ",\"feature_schema\":" << Json(SpecFor(app).feature_schema) << ",\"operation_label\":" << Json(prediction.operation_label)
        << ",\"cosine_similarity\":" << prediction.cosine_similarity << ",\"center_id\":" << prediction.center_id
        << ",\"accuracy_evaluated\":false}\n";
    return out.str();
}
void Stages(const fs::path& path, const Replay& replay) {
    if (fs::exists(path)) throw std::runtime_error("stages directory must not exist");
    fs::create_directories(path);
    for (std::size_t i = 0; i < replay.baselines.size(); ++i) NewFile(path / ("baseline" + std::to_string(i) + ".csv"), MapCsv(replay.baselines[i]));
    NewFile(path / "action.csv", MapCsv(replay.action));
    NewFile(path / "post.csv", MapCsv(replay.post));
    NewFile(path / "median.csv", MapCsv(BaselineMedian(replay.baselines)));
    NewFile(path / "excess.csv", MapCsv(ExcessPages(replay.baselines, replay.action, replay.post)));
}
void Selftest(const fs::path& models, const fs::path& fixtures) {
    for (const std::string app : {"wps", "bilibili", "huawei_browser"}) {
        const auto replay = LoadRawReplay(fixtures / "manifest.csv", app);
        const auto prediction = CentroidModel::Load(models, app, SpecFor(app).feature_schema).Predict(replay.vector);
        std::ifstream input(fixtures / (app + "_expected.csv"));
        std::vector<std::string> fields;
        if (!input || !ReadCsvRow(input, fields) || fields.size() != 2050 || !ReadCsvRow(input, fields) || fields.size() != 2050)
            throw std::runtime_error("missing/invalid frozen Python golden fixture");
        double delta = 0;
        for (std::size_t i = 0; i < kVectorDimension; ++i) delta = std::max(delta, std::abs(replay.vector[i] - ParseFiniteDouble(fields[i+2], "golden vector")));
        if (fields[0] != prediction.operation_label || std::abs(prediction.cosine_similarity - ParseFiniteDouble(fields[1], "golden score")) > 1e-9 || delta > 1e-9)
            throw std::runtime_error("raw replay diverges from frozen Python golden: " + app);
    }
    std::cout << "{\"schema\":\"harmony.ops.selftest.v1\",\"status\":\"ok\",\"apps\":3,\"fixture\":\"synthetic\",\"tolerance\":1e-9,\"accuracy_evaluated\":false}\n";
}
}  // namespace

int main(int argc, char** argv) {
    try {
        if (argc < 2 || std::string(argv[1]) == "--help") {
            std::cout << "opsctl doctor --models DIR [--app APP] [--pid PID ...]\n"
                         "opsctl selftest --models DIR --fixtures DIR\n"
                         "opsctl replay --models DIR --app APP --manifest CSV --output NEW_JSON [--vector-output NEW_CSV] [--stages NEW_DIR]\n"
                         "opsctl run --config FILE --window-id ID --event-id ID [--output NEW_JSONL]\n";
            std::cout << "opsctl serve --config FILE [--output NEW_JSONL]  # stdin: window_id<TAB>event_id; STOP or EOF ends\n";
            return argc < 2 ? 2 : 0;
        }
        const std::string command = argv[1];
        std::map<std::string, std::string> options;
        std::vector<std::int32_t> pids;
        const std::map<std::string, std::set<std::string>> allowed{
            {"doctor",{"--models","--app","--pid"}}, {"selftest",{"--models","--fixtures"}},
            {"replay",{"--models","--app","--manifest","--output","--vector-output","--stages"}},
            {"run",{"--config","--window-id","--event-id","--output"}},
            {"serve",{"--config","--output"}}};
        if (!allowed.count(command)) throw std::invalid_argument("unknown command");
        for (int i = 2; i < argc; ++i) {
            const std::string key = argv[i];
            if (!allowed.at(command).count(key) || ++i >= argc) throw std::invalid_argument("invalid or incomplete option: " + key);
            if (key == "--pid") {
                const auto pid = Unsigned(argv[i]);
                if (!pid || pid > 2147483647) throw std::invalid_argument("invalid PID");
                pids.push_back(static_cast<std::int32_t>(pid));
            } else if (!options.emplace(key, argv[i]).second) throw std::invalid_argument("duplicate option: " + key);
        }
        const auto required = [&](const std::string& key) -> const std::string& {
            const auto it = options.find(key);
            if (it == options.end() || it->second.empty()) throw std::invalid_argument("required: " + key);
            return it->second;
        };
        if (command == "doctor") {
            const fs::path models = required("--models");
            const auto apps = options.count("--app") ? std::vector<std::string>{options.at("--app")} : std::vector<std::string>{"wps","bilibili","huawei_browser"};
            for (const auto& app : apps) (void)CentroidModel::Load(models, app, SpecFor(app).feature_schema);
            for (const auto pid : pids) {
                hc_process_identity identity{}; hc_error error{};
                auto status = hc_read_identity(pid, &identity, &error);
                if (status != HC_OK) throw std::runtime_error(std::string(hc_status_name(status)) + ": " + error.message);
                hc_snapshot snapshot{};
                status = hc_collect(&identity, HC_SEGMENT_WPS, &snapshot, &error);
                hc_snapshot_free(&snapshot);
                if (status != HC_OK) throw std::runtime_error(std::string(hc_status_name(status)) + ": " + error.message);
            }
            std::cout << "{\"schema\":\"harmony.ops.doctor.v1\",\"status\":\"ok\",\"models\":" << apps.size()
                      << ",\"procfs_read_targets\":" << pids.size() << ",\"clear_refs_tested\":false,\"focus_tested\":false,\"device_validated\":false}\n";
        } else if (command == "selftest") Selftest(required("--models"), required("--fixtures"));
        else if (command == "replay") {
            const fs::path destination = required("--output");
            const auto app = required("--app");
            const auto replay = LoadRawReplay(required("--manifest"), app);
            const auto prediction = CentroidModel::Load(required("--models"), app, SpecFor(app).feature_schema).Predict(replay.vector);
            // Validate all output destinations before writing any successful result.
            for (const auto key : {"--output", "--vector-output", "--stages"}) if (options.count(key) && fs::exists(options.at(key))) throw std::runtime_error(std::string("output exists: ") + key);
            if (options.count("--stages")) Stages(options.at("--stages"), replay);
            if (options.count("--vector-output")) NewFile(options.at("--vector-output"), VectorCsv(replay.vector));
            NewFile(destination, PredictionJson(app, prediction));
            std::cout << PredictionJson(app, prediction);
        } else return RunPlatformControlled(required("--config"), command == "run" ? required("--window-id") : "", command == "run" ? required("--event-id") : "", options.count("--output") ? fs::path(options.at("--output")) : fs::path{}, command == "serve");
        std::cout.flush();
        if (!std::cout) throw std::runtime_error("diagnostic output failed");
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "{\"schema\":\"harmony.ops.error.v1\",\"status\":\"error\",\"message\":" << Json(error.what()) << "}\n";
        return 2;
    }
}
