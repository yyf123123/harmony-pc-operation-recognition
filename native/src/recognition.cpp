#include "harmony_ops/recognition.hpp"

#include <cmath>
#include <fstream>
#include <iterator>
#include <limits>
#include <stdexcept>

namespace harmony_ops {
namespace {

const ModelSpec kBilibili{
    "bilibili", "cross_app.vma.feature.v2",
    {"back_to_feed", "open_comments", "open_fixed_video", "open_search",
     "open_uploader_profile", "to_background"},
    "0c0b171bca7b7e1d861e15da3b4845ebf66cbbe899d7cd6e02fab809e0068337",
    "51f93ac0895c3e8900f600ce01d1aa3c27394d7836708c04790c1b03674d8f80"};
const ModelSpec kBrowser{
    "huawei_browser", "cross_app.vma.feature.v2",
    {"navigate_fixed_url", "open_downloads_page", "open_history_page",
     "open_settings", "open_tab_overview"},
    "0836b119d00326c4a8e419050d5306cbf33c4abc0f8a5bb1fd70c9444dc28c08",
    "718b6b7d3bd37d8cbd41ba753aa1a7b100c932c17419848aaf29a3fe48a3d878"};
const ModelSpec kWps{
    "wps", "wps.vma.feature.v1",
    {"ALIGN_CENTER", "CLOSE_DOCUMENT", "COPY_SELECTION", "CUT_SELECTION",
     "FIND_TEXT", "FORMAT_BOLD", "FORMAT_ITALIC", "FORMAT_UNDERLINE",
     "INSERT_PAGE_BREAK", "INSERT_TABLE", "NEW_DOCUMENT", "PASTE_SELECTION",
     "REDO_EDIT", "REPLACE_TEXT", "SAVE_DOCUMENT", "SELECT_ALL",
     "UNDO_EDIT", "WRITE_TEXT"},
    "2ca6bcce2856798c0998c28414053d9338e28a61923c50184c3f228a7cd42c7f",
    "dbd1277f21252f2827ec965a2b195365b872b0f1eae4c7b2eb709d7aaba233ea"};

std::vector<std::string> CenterHeader() {
    std::vector<std::string> fields{"center_id", "operation_label"};
    for (std::size_t i = 0; i < kVectorDimension; ++i) {
        std::string column = std::to_string(i);
        fields.push_back("v" + std::string(4 - column.size(), '0') + column);
    }
    return fields;
}

}  // namespace

const ModelSpec& SpecFor(const std::string& app_id) {
    if (app_id == kBilibili.app_id) return kBilibili;
    if (app_id == kBrowser.app_id) return kBrowser;
    if (app_id == kWps.app_id) return kWps;
    throw std::invalid_argument("unsupported app_id: " + app_id);
}

std::vector<double> NormalizeNonzero(const std::vector<double>& vector) {
    if (vector.size() != kVectorDimension) {
        throw std::invalid_argument("expected exactly 2048 vector values");
    }
    double sum = 0.0;
    for (double value : vector) {
        if (!std::isfinite(value)) throw std::invalid_argument("non-finite vector value");
        sum += value * value;
    }
    const double norm = std::sqrt(sum);
    if (!std::isfinite(norm) || norm <= 0.0)
        throw std::invalid_argument("zero or invalid vector norm");
    std::vector<double> normalized;
    normalized.reserve(vector.size());
    for (double value : vector) normalized.push_back(value / norm);
    return normalized;
}

CentroidModel CentroidModel::Load(const std::filesystem::path& models_root,
                                  const std::string& app_id,
                                  const std::string& feature_schema) {
    const ModelSpec& spec = SpecFor(app_id);
    if (feature_schema != spec.feature_schema) {
        throw std::invalid_argument("feature schema mismatch for " + app_id +
                                    ": expected " + spec.feature_schema);
    }
    const auto metadata_path = models_root / app_id / "model.json";
    const auto path = models_root / app_id / "centers.csv";
    std::ifstream metadata(metadata_path, std::ios::binary);
    if (!metadata) throw std::runtime_error("cannot open model metadata: " + metadata_path.string());
    const std::string metadata_bytes((std::istreambuf_iterator<char>(metadata)),
                                     std::istreambuf_iterator<char>());
    if (Sha256Hex(metadata_bytes) != spec.model_sha256)
        throw std::runtime_error("model.json SHA-256 mismatch");
    std::ifstream input(path, std::ios::binary);
    if (!input) throw std::runtime_error("cannot open center file: " + path.string());
    const std::string center_bytes((std::istreambuf_iterator<char>(input)),
                                   std::istreambuf_iterator<char>());
    if (Sha256Hex(center_bytes) != spec.centers_sha256)
        throw std::runtime_error("centers.csv SHA-256 mismatch");
    input.clear();
    input.seekg(0);
    std::vector<std::string> row;
    if (!ReadCsvRow(input, row) || row != CenterHeader())
        throw std::runtime_error("invalid center CSV header: " + path.string());
    CentroidModel model(&spec);
    for (std::size_t center_id = 0; ReadCsvRow(input, row); ++center_id) {
        if (center_id >= spec.center_labels.size() || row.size() != kVectorDimension + 2 ||
            row[0] != std::to_string(center_id) || row[1] != spec.center_labels[center_id]) {
            throw std::runtime_error("center ID, label mapping or dimension mismatch");
        }
        std::vector<double> values;
        values.reserve(kVectorDimension);
        for (std::size_t i = 0; i < kVectorDimension; ++i)
            values.push_back(ParseFiniteDouble(row[i + 2], "center value"));
        model.centers_.push_back(NormalizeNonzero(values));
    }
    if (model.centers_.size() != spec.center_labels.size())
        throw std::runtime_error("wrong center count");
    return model;
}

Prediction CentroidModel::Predict(const std::vector<double>& vector) const {
    const auto normalized = NormalizeNonzero(vector);
    double best = -std::numeric_limits<double>::infinity();
    std::size_t best_id = 0;
    for (std::size_t i = 0; i < centers_.size(); ++i) {
        double score = 0.0;
        for (std::size_t j = 0; j < kVectorDimension; ++j)
            score += normalized[j] * centers_[i][j];
        if (score > best) {  // NumPy argmax also keeps the first tie.
            best = score;
            best_id = i;
        }
    }
    return {spec_->center_labels[best_id], best, best_id};
}

bool ReadCsvRow(std::istream& input, std::vector<std::string>& fields) {
    fields.clear();
    std::string field;
    bool quoted = false;
    bool closed_quote = false;
    bool any = false;
    for (;;) {
        const int next = input.get();
        if (next == EOF) {
            if (quoted) throw std::runtime_error("unterminated CSV quote");
            if (!any) return false;
            fields.push_back(field);
            return true;
        }
        any = true;
        const char c = static_cast<char>(next);
        if (quoted) {
            if (c == '"') {
                if (input.peek() == '"') { input.get(); field.push_back('"'); }
                else { quoted = false; closed_quote = true; }
            } else field.push_back(c);
        } else if (c == ',') {
            fields.push_back(field);
            field.clear();
            closed_quote = false;
        } else if (c == '\n' || c == '\r') {
            if (c == '\r' && input.peek() == '\n') input.get();
            fields.push_back(field);
            return true;
        } else if (c == '"' && field.empty() && !closed_quote) {
            quoted = true;
        } else {
            if (closed_quote || c == '"') throw std::runtime_error("malformed CSV quote");
            field.push_back(c);
        }
    }
}

void WriteCsvField(std::ostream& output, const std::string& field) {
    if (field.find_first_of(",\"\r\n") == std::string::npos) {
        output << field;
        return;
    }
    output << '"';
    for (char c : field) {
        if (c == '"') output << '"';
        output << c;
    }
    output << '"';
}

double ParseFiniteDouble(const std::string& text, const std::string& context) {
    std::size_t end = 0;
    double value;
    try { value = std::stod(text, &end); }
    catch (const std::exception&) { throw std::invalid_argument(context + ": nonnumeric value"); }
    if (end != text.size() || !std::isfinite(value))
        throw std::invalid_argument(context + ": non-finite or trailing text");
    return value;
}

}  // namespace harmony_ops
