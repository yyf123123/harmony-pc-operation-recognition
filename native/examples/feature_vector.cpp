#include "harmony_ops/feature.hpp"

#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <stdexcept>

namespace fs = std::filesystem;
using harmony_ops::FeatureMap;

namespace {

std::string Value(int& index, int argc, char** argv) {
    if (++index >= argc) throw std::invalid_argument("missing argument value");
    return argv[index];
}

FeatureMap ReadFeatures(const fs::path& path) {
    std::ifstream input(path, std::ios::binary);
    if (!input) throw std::runtime_error("cannot open feature map: " + path.string());
    std::vector<std::string> row;
    if (!harmony_ops::ReadCsvRow(input, row) ||
        row != std::vector<std::string>{"namespace", "feature_key", "pages"})
        throw std::runtime_error("feature map header must be namespace,feature_key,pages");
    FeatureMap result;
    while (harmony_ops::ReadCsvRow(input, row)) {
        if (row.size() != 3 || row[1].empty() ||
            (row[0] != "FILE" && row[0] != "ANON"))
            throw std::runtime_error("invalid feature map row");
        const auto key = harmony_ops::FeatureKey{row[0], row[1]};
        if (!result.emplace(key, harmony_ops::ParseFiniteDouble(row[2], "pages")).second)
            throw std::runtime_error("duplicate semantic feature key");
    }
    return result;
}

}  // namespace

int main(int argc, char** argv) {
    try {
        std::string schema, sample_id;
        fs::path input, action, post, output;
        std::vector<fs::path> baselines;
        for (int i = 1; i < argc; ++i) {
            const std::string arg = argv[i];
            if (arg == "--schema") schema = Value(i, argc, argv);
            else if (arg == "--sample-id") sample_id = Value(i, argc, argv);
            else if (arg == "--input") input = Value(i, argc, argv);
            else if (arg == "--baseline") baselines.emplace_back(Value(i, argc, argv));
            else if (arg == "--action") action = Value(i, argc, argv);
            else if (arg == "--post") post = Value(i, argc, argv);
            else if (arg == "--output") output = Value(i, argc, argv);
            else if (arg == "--help") {
                std::cout << "operation_feature --schema wps.vma.feature.v1 --sample-id ID "
                             "--input AGGREGATED_CSV --output NEW_CSV\n"
                             "operation_feature --schema cross_app.vma.feature.v2 --sample-id ID "
                             "--baseline CSV [--baseline CSV] --action CSV --post CSV --output NEW_CSV\n";
                return 0;
            } else throw std::invalid_argument("unknown argument: " + arg);
        }
        if (sample_id.empty() || output.empty() || fs::exists(output))
            throw std::invalid_argument("--sample-id and a new --output file are required");
        std::vector<double> vector;
        if (schema == "wps.vma.feature.v1") {
            if (input.empty() || !baselines.empty() || !action.empty() || !post.empty())
                throw std::invalid_argument("WPS needs only --input aggregated features");
            vector = harmony_ops::BuildWpsVector(ReadFeatures(input));
        } else if (schema == "cross_app.vma.feature.v2") {
            if (!input.empty() || baselines.empty() || action.empty() || post.empty())
                throw std::invalid_argument("v2 needs --baseline, --action and --post maps");
            std::vector<FeatureMap> maps;
            for (const auto& path : baselines) maps.push_back(ReadFeatures(path));
            vector = harmony_ops::BuildCrossAppV2Vector(maps, ReadFeatures(action), ReadFeatures(post));
        } else throw std::invalid_argument("unsupported feature schema; signed v3 is 8192-D");
        fs::create_directories(output.parent_path().empty() ? fs::path(".") : output.parent_path());
        std::ofstream result(output, std::ios::binary);
        if (!result) throw std::runtime_error("cannot open output CSV");
        result << "sample_id";
        for (std::size_t i = 0; i < harmony_ops::kVectorDimension; ++i) {
            const auto number = std::to_string(i);
            result << ",v" << std::string(4 - number.size(), '0') << number;
        }
        result << '\n';
        harmony_ops::WriteCsvField(result, sample_id);
        result << std::setprecision(17);
        for (double value : vector) result << ',' << value;
        result << '\n';
        if (!result) throw std::runtime_error("failed to write feature CSV");
        std::cout << "constructed " << vector.size() << " features\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "operation_feature: " << error.what() << '\n';
        return 2;
    }
}
