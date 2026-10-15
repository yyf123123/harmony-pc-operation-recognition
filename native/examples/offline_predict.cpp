#include "harmony_ops/recognition.hpp"

#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <set>
#include <stdexcept>

namespace fs = std::filesystem;
using harmony_ops::kVectorDimension;

namespace {

std::string Value(int& index, int argc, char** argv) {
    if (++index >= argc) throw std::invalid_argument("missing argument value");
    return argv[index];
}

std::vector<std::string> VectorHeader() {
    std::vector<std::string> header{"sample_id"};
    for (std::size_t i = 0; i < kVectorDimension; ++i) {
        const auto number = std::to_string(i);
        header.push_back("v" + std::string(4 - number.size(), '0') + number);
    }
    return header;
}

}  // namespace

int main(int argc, char** argv) {
    try {
        fs::path models, vectors, output;
        std::string app, schema;
        for (int i = 1; i < argc; ++i) {
            const std::string arg = argv[i];
            if (arg == "--models") models = Value(i, argc, argv);
            else if (arg == "--vectors") vectors = Value(i, argc, argv);
            else if (arg == "--output") output = Value(i, argc, argv);
            else if (arg == "--app") app = Value(i, argc, argv);
            else if (arg == "--schema") schema = Value(i, argc, argv);
            else if (arg == "--help") {
                std::cout << "operation_predict --models DIR --app APP --schema SCHEMA "
                             "--vectors CSV --output NEW_CSV\n";
                return 0;
            } else throw std::invalid_argument("unknown argument: " + arg);
        }
        if (models.empty() || app.empty() || schema.empty() || vectors.empty() || output.empty())
            throw std::invalid_argument("--models, --app, --schema, --vectors and --output are required");
        if (fs::absolute(vectors).lexically_normal() == fs::absolute(output).lexically_normal() ||
            fs::exists(output))
            throw std::invalid_argument("output must be a new file distinct from the input");
        const auto model = harmony_ops::CentroidModel::Load(models, app, schema);
        std::ifstream input(vectors, std::ios::binary);
        if (!input) throw std::runtime_error("cannot open vector CSV");
        std::vector<std::string> row;
        if (!harmony_ops::ReadCsvRow(input, row)) throw std::runtime_error("empty vector CSV");
        if (!row.empty() && row[0].rfind("\xEF\xBB\xBF", 0) == 0) row[0].erase(0, 3);
        if (row != VectorHeader()) throw std::runtime_error("expected sample_id and 2048 ordered columns");
        std::set<std::string> ids;
        std::vector<std::pair<std::string, harmony_ops::Prediction>> predictions;
        while (harmony_ops::ReadCsvRow(input, row)) {
            if (row.size() != kVectorDimension + 1 || row[0].empty() || !ids.insert(row[0]).second)
                throw std::runtime_error("invalid or duplicate sample ID / vector dimension");
            std::vector<double> vector;
            vector.reserve(kVectorDimension);
            for (std::size_t i = 0; i < kVectorDimension; ++i)
                vector.push_back(harmony_ops::ParseFiniteDouble(row[i + 1], "input vector"));
            predictions.emplace_back(row[0], model.Predict(vector));
        }
        fs::create_directories(output.parent_path().empty() ? fs::path(".") : output.parent_path());
        std::ofstream result(output, std::ios::binary | std::ios::out | std::ios::trunc);
        if (!result) throw std::runtime_error("cannot open output CSV");
        result << "sample_id,operation_label,cosine_similarity\n" << std::setprecision(17);
        for (const auto& [sample_id, prediction] : predictions) {
            harmony_ops::WriteCsvField(result, sample_id);
            result << ',';
            harmony_ops::WriteCsvField(result, prediction.operation_label);
            result << ',' << prediction.cosine_similarity << '\n';
        }
        if (!result) throw std::runtime_error("failed to write output CSV");
        std::cout << "predicted " << predictions.size() << " samples for " << app << '\n';
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "operation_predict: " << error.what() << '\n';
        return 2;
    }
}
