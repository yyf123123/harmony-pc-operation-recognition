#include "harmony_ops/replay.hpp"
#include "harmony_ops/runtime.hpp"

#include <cmath>
#include <fstream>
#include <map>
#include <set>
#include <sstream>
#include <stdexcept>
#include <tuple>

namespace harmony_ops {
namespace {
std::uint64_t Unsigned(const std::string& text) {
    if (text.empty() || text.find_first_not_of("0123456789") != std::string::npos)
        throw std::invalid_argument("raw replay expected an unsigned integer");
    std::size_t count = 0;
    const auto result = std::stoull(text, &count);
    if (count != text.size()) throw std::invalid_argument("raw replay invalid integer");
    return result;
}
std::string Read(const std::filesystem::path& path) {
    std::ifstream input(path, std::ios::binary);
    if (!input) throw RuntimeError(RuntimeCode::CollectionFailed,
                                  "cannot open replay input: " + path.string());
    std::ostringstream buffer;
    buffer << input.rdbuf();
    if (input.bad() || buffer.bad())
        throw RuntimeError(RuntimeCode::CollectionFailed,
                           "failed replay input read: " + path.string());
    return buffer.str();
}
} // namespace

ReplayWindow LoadRawReplay(const std::filesystem::path& manifest,
                           const std::string& app_id) {
    const auto& schema = SpecFor(app_id).feature_schema;
    std::ifstream input(manifest, std::ios::binary);
    std::vector<std::string> row;
    if (!input || !ReadCsvRow(input, row) ||
        row != std::vector<std::string>{"phase", "index", "pid", "page_size", "smaps"})
        throw std::invalid_argument("invalid raw replay manifest header");
    using Phase = std::pair<std::string, std::size_t>;
    std::map<Phase, FeatureMap> phases;
    std::map<Phase, std::set<std::int32_t>> coverage;
    std::set<std::tuple<std::string, std::size_t, std::uint64_t>> seen;
    ReplayWindow result;
    while (ReadCsvRow(input, row)) {
        if (row.size() != 5 || row[4].empty() || row[4].find('\0') != std::string::npos)
            throw std::invalid_argument("invalid raw replay manifest row");
        const auto index = Unsigned(row[1]), pid = Unsigned(row[2]), size = Unsigned(row[3]);
        if (pid == 0 || pid > 2147483647 || index >= 1024 ||
            (row[0] != "baseline" && row[0] != "action" && row[0] != "post") ||
            (row[0] != "baseline" && index != 0))
            throw std::invalid_argument("invalid phase/index/PID in raw replay manifest");
        if (!size || (result.page_size && size != result.page_size))
            throw std::invalid_argument("inconsistent original page sizes in raw replay");
        result.page_size = size;
        if (!seen.emplace(row[0], static_cast<std::size_t>(index), pid).second)
            throw std::invalid_argument("duplicate phase/PID in raw replay");
        const auto text = Read(manifest.parent_path() / std::filesystem::u8path(row[4]));
        hc_snapshot snapshot{};
        hc_error error{};
        const auto status = hc_parse_smaps(text.data(), text.size(), size,
            app_id == "wps" ? HC_SEGMENT_WPS : HC_SEGMENT_CROSS_APP, &snapshot, &error);
        if (status != HC_OK)
            throw RuntimeError(RuntimeCode::CollectionFailed,
                std::string(hc_status_name(status)) + ": " + error.message);
        FeatureMap map;
        try { map = SemanticFeatures(snapshot, schema); }
        catch (...) { hc_snapshot_free(&snapshot); throw; }
        hc_snapshot_free(&snapshot);
        auto& combined = phases[{row[0], static_cast<std::size_t>(index)}];
        for (const auto& [key, value] : map) {
            auto& total = combined[key];
            total += value;
            if (!std::isfinite(total))
                throw std::invalid_argument("raw replay process aggregation overflow");
        }
        coverage[{row[0], static_cast<std::size_t>(index)}].insert(static_cast<std::int32_t>(pid));
    }
    if (input.bad()) throw RuntimeError(RuntimeCode::CollectionFailed, "raw replay manifest read failed");
    const auto action = phases.find({"action", 0}), post = phases.find({"post", 0});
    if (action == phases.end() || post == phases.end())
        throw std::invalid_argument("missing action/post in raw replay");
    for (std::size_t index = 0; phases.count({"baseline", index}); ++index)
        result.baselines.push_back(phases.at({"baseline", index}));
    if (result.baselines.size() < 2 || phases.size() != result.baselines.size() + 2)
        throw std::invalid_argument("raw replay needs contiguous complete baseline windows");
    const auto& expected = coverage.at({"action", 0});
    for (const auto& [phase, selected] : coverage) {
        (void)phase;
        if (selected != expected)
            throw std::invalid_argument("process coverage changed in raw replay");
    }
    result.covered_pids.assign(expected.begin(), expected.end());
    result.action = action->second;
    result.post = post->second;
    result.vector = BuildWindowVector(schema, result.baselines, result.action, result.post);
    return result;
}
} // namespace harmony_ops
