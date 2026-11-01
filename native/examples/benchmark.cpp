#include "harmony_ops/replay.hpp"
#include "harmony_ops/runtime.hpp"

#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <set>
#include <stdexcept>

namespace fs=std::filesystem;
using namespace harmony_ops;

int main(int argc,char** argv) {
    try {
        fs::path models,manifest,output;
        std::string profile="frozen";
        unsigned repeats=5,warmup=1;
        for (int i=1;i<argc;++i) {
            const std::string key=argv[i];
            if (key=="--help") {
                std::cout << "operation_benchmark --models DIR --manifest CSV --output NEW_DIR [--model-profile PROFILE] [--repeats 5] [--warmup 1]\n";
                return 0;
            }
            if (++i>=argc) throw std::invalid_argument("missing benchmark argument value");
            const std::string value=argv[i];
            if (key=="--models") models=value;
            else if (key=="--manifest") manifest=value;
            else if (key=="--output") output=value;
            else if (key=="--model-profile") profile=value;
            else if (key=="--repeats" || key=="--warmup") {
                if (value.empty() || value.find_first_not_of("0123456789")!=std::string::npos)
                    throw std::invalid_argument("benchmark count must be an integer");
                const auto n=std::stoul(value);
                if (!n || n>100) throw std::invalid_argument("benchmark counts must be 1..100");
                (key=="--repeats"?repeats:warmup)=static_cast<unsigned>(n);
            } else throw std::invalid_argument("unknown benchmark argument");
        }
        if (models.empty() || manifest.empty() || output.empty() || fs::exists(output))
            throw std::invalid_argument("models/manifest and a new output directory required");
        std::ifstream master(manifest,std::ios::binary);
        std::vector<std::string> row;
        if (!master || !ReadCsvRow(master,row) || row!=std::vector<std::string>{"sample_id","window_manifest"})
            throw std::invalid_argument("benchmark manifest header must be sample_id,window_manifest");
        std::vector<std::pair<std::string,fs::path>> windows;
        std::set<std::string> seen;
        while (ReadCsvRow(master,row)) {
            if (row.size()!=2 || row[0].empty() || row[1].empty() || !seen.insert(row[0]).second || windows.size()>=10000)
                throw std::invalid_argument("invalid/duplicate benchmark observation");
            windows.emplace_back(row[0],manifest.parent_path()/fs::u8path(row[1]));
        }
        if (windows.empty() || master.bad()) throw std::invalid_argument("empty/unreadable benchmark manifest");
        const auto load_begin=MonotonicNowNs();
        const auto model=CentroidModel::Load(models,"wps",SpecFor("wps").feature_schema,profile);
        const auto load_ns=MonotonicNowNs()-load_begin;
        // Validate every input and warm the same immutable workload before any
        // successful-looking output is created. No OS cache clearing occurs.
        for (unsigned round=0;round<warmup;++round)
            for (const auto& [id,path]:windows) {
                (void)id;
                (void)model.Predict(LoadCompactReplay(path,"wps").vector);
            }
        fs::create_directories(output);
        std::ofstream timing(output/"timings.csv"),predictions(output/"predictions.csv"),vectors(output/"vectors.csv"),published(output/"results.jsonl");
        if (!timing || !predictions || !vectors || !published) throw std::runtime_error("benchmark output unavailable");
        timing << "sample_id,repeat,file_read_ns,compact_parse_ns,semantic_ns,baseline_ns,excess_ns,vector_ns,inference_ns,output_ns,total_ns\n";
        predictions << "sample_id,operation_label,cosine_similarity\n" << std::setprecision(17);
        vectors << "sample_id" << std::setprecision(17);
        for (std::size_t i=0;i<kVectorDimension;++i) vectors << ",v" << std::setw(4) << std::setfill('0') << i;
        vectors << '\n';
        for (unsigned round=0;round<repeats;++round) for (const auto& [id,path]:windows) {
            const auto begin=MonotonicNowNs();
            const auto replay=LoadCompactReplay(path,"wps");
            const auto infer_begin=MonotonicNowNs();
            const auto prediction=model.Predict(replay.vector);
            const auto infer_ns=MonotonicNowNs()-infer_begin;
            const auto output_begin=MonotonicNowNs();
            published << std::setprecision(17) << "{\"schema\":\"harmony.ops.benchmark-result.v1\",\"source\":\"offline_compact_replay\",\"sample_id\":" << JsonString(id)
                << ",\"repeat\":" << round << ",\"model_profile\":" << JsonString(profile)
                << ",\"operation_label\":" << JsonString(prediction.operation_label) << ",\"cosine_similarity\":" << prediction.cosine_similarity << "}\n";
            published.flush(); // page-cache flush, not fsync or a host IPC channel
            if (!published) throw std::runtime_error("benchmark result output failed");
            const auto output_ns=MonotonicNowNs()-output_begin,total_ns=MonotonicNowNs()-begin;
            const auto& t=replay.timing;
            WriteCsvField(timing,id);
            timing << ',' << round << ',' << t.file_read_ns << ',' << t.compact_parse_ns << ',' << t.semantic_ns
                << ',' << t.baseline_ns << ',' << t.excess_ns << ',' << t.vector_ns << ',' << infer_ns << ',' << output_ns << ',' << total_ns << '\n';
            if (!round) {
                WriteCsvField(predictions,id); predictions << ',';
                WriteCsvField(predictions,prediction.operation_label); predictions << ',' << prediction.cosine_similarity << '\n';
                WriteCsvField(vectors,id);
                for (double value:replay.vector) vectors << ',' << value;
                vectors << '\n';
            }
        }
        timing.flush(); predictions.flush(); vectors.flush(); published.flush();
        if (!timing || !predictions || !vectors || !published) throw std::runtime_error("benchmark diagnostic output failed");
        timing.close(); predictions.close(); vectors.close(); published.close();
        if (!timing || !predictions || !vectors || !published) throw std::runtime_error("benchmark output close failed");
        std::cout << "{\"schema\":\"harmony.ops.benchmark.v1\",\"status\":\"ok\",\"source\":\"offline_compact_replay\",\"model_profile\":" << JsonString(profile)
            << ",\"observations\":" << windows.size() << ",\"repeats\":" << repeats << ",\"warmup_passes\":" << warmup
            << ",\"model_load_ns\":" << load_ns << ",\"cache_cleared\":false,\"output_flush\":\"ostream_flush_no_fsync\",\"diagnostic_csv_overhead_included\":false}\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "operation_benchmark: " << error.what() << '\n';
        return 2;
    }
}
