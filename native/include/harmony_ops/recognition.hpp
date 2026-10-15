#pragma once

#include <cstddef>
#include <filesystem>
#include <iosfwd>
#include <string>
#include <vector>

namespace harmony_ops {

constexpr std::size_t kVectorDimension = 2048;

struct ModelSpec {
    std::string app_id;
    std::string feature_schema;
    std::vector<std::string> center_labels;
    std::string model_sha256;
    std::string centers_sha256;
};

// Frozen deployment candidates copied from models/*/model.json. No threshold is fitted.
const ModelSpec& SpecFor(const std::string& app_id);

struct Prediction {
    std::string operation_label;
    double cosine_similarity;
    std::size_t center_id;
};

class CentroidModel {
public:
    static CentroidModel Load(const std::filesystem::path& models_root,
                              const std::string& app_id,
                              const std::string& feature_schema);
    Prediction Predict(const std::vector<double>& vector) const;
    const ModelSpec& spec() const noexcept { return *spec_; }

private:
    explicit CentroidModel(const ModelSpec* spec) : spec_(spec) {}
    const ModelSpec* spec_;
    std::vector<std::vector<double>> centers_;
};

// Shared by inference and the portable feature builder. Zero and non-finite
// vectors are rejected, matching the Python deployment inference path.
std::vector<double> NormalizeNonzero(const std::vector<double>& vector);

// Small RFC 4180-style CSV helpers used by the command-line examples.
bool ReadCsvRow(std::istream& input, std::vector<std::string>& fields);
void WriteCsvField(std::ostream& output, const std::string& field);
double ParseFiniteDouble(const std::string& text, const std::string& context);
std::string Sha256Hex(const std::string& bytes);

}  // namespace harmony_ops
