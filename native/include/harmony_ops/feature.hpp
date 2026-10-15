#pragma once

#include <cstddef>
#include <map>
#include <string>
#include <utility>
#include <vector>

#include "harmony_ops/recognition.hpp"

namespace harmony_ops {

using FeatureKey = std::pair<std::string, std::string>;  // FILE/ANON, semantic key
using FeatureMap = std::map<FeatureKey, double>;          // referenced excess pages

// The first eight SHA-256 bytes of "NAMESPACE:feature_key", big endian,
// modulo 1024. This matches features/vma_feature.py and features/wps.py.
std::size_t StableHashIndex(const std::string& name_space,
                            const std::string& feature_key);

// WPS receives already aggregated positive excess pages. The output is the
// frozen wps.vma.feature.v1 layout. It does not parse device reports.
std::vector<double> BuildWpsVector(const FeatureMap& excess_pages);

// Cross-app v2 receives semantic FILE/ANON referenced-page maps for complete
// baseline, action and post-action windows. Missing keys count as zero.
std::vector<double> BuildCrossAppV2Vector(const std::vector<FeatureMap>& baselines,
                                           const FeatureMap& action,
                                           const FeatureMap& post_action);

}  // namespace harmony_ops
