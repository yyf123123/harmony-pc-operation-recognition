#pragma once

#include "harmony_ops/feature.hpp"

#include <cstdint>
#include <filesystem>
#include <string>
#include <vector>

namespace harmony_ops {

struct ReplayWindow {
    struct Timing {
        std::uint64_t file_read_ns=0, compact_parse_ns=0, semantic_ns=0;
        std::uint64_t baseline_ns=0, excess_ns=0, vector_ns=0, total_ns=0;
    } timing;
    std::vector<FeatureMap> baselines;
    FeatureMap action, post;
    std::vector<double> vector;
    std::uint64_t page_size = 0;
    std::vector<std::int32_t> covered_pids;
};

// Pure offline path shared by opsctl and an existing host service. No focus,
// clear_refs, clock, callbacks, history, or model changes occur. The UTF-8 CSV
// manifest header is phase,index,pid,page_size,smaps. Relative smaps filenames
// resolve against the manifest directory; absolute paths remain explicit.
// Requires >=2 contiguous baselines, action/post index 0, one row per phase/PID,
// the same PID coverage and actual page size throughout, and complete strict
// Referenced text for every row. Frozen schema-specific transforms are reused.
// Offline PIDs are provenance only: this API cannot establish live process
// identity or validate an operation label. Call CentroidModel::Predict on vector
// for the same pinned model validation as the controlled runtime.
ReplayWindow LoadRawReplay(const std::filesystem::path& manifest,
                           const std::string& app_id);
// WPS collect.c compact TSV replay, no procfs or live identity access. Manifest:
// phase,index,pid,page_size,compact_vma. Each TSV must cover exactly its row PID;
// split multi-process TSVs per PID before creating the manifest.
ReplayWindow LoadCompactReplay(const std::filesystem::path& manifest,
                               const std::string& app_id);

}  // namespace harmony_ops
