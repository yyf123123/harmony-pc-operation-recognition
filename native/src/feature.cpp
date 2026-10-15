#include "harmony_ops/feature.hpp"

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <stdexcept>
#include <vector>

namespace harmony_ops {
namespace {

constexpr std::array<std::uint32_t, 64> kRound{
    0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
    0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
    0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
    0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
    0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
    0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
    0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
    0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2};

std::uint32_t Rotate(std::uint32_t value, unsigned bits) {
    return (value >> bits) | (value << (32 - bits));
}

std::array<std::uint8_t, 32> Sha256(const std::string& input) {
    std::vector<std::uint8_t> bytes(input.begin(), input.end());
    const std::uint64_t bit_count = static_cast<std::uint64_t>(bytes.size()) * 8;
    bytes.push_back(0x80);
    while (bytes.size() % 64 != 56) bytes.push_back(0);
    for (int shift = 56; shift >= 0; shift -= 8)
        bytes.push_back(static_cast<std::uint8_t>(bit_count >> shift));
    std::array<std::uint32_t, 8> h{0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,
                                    0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19};
    for (std::size_t offset = 0; offset < bytes.size(); offset += 64) {
        std::array<std::uint32_t, 64> w{};
        for (std::size_t i = 0; i < 16; ++i) {
            const auto at = offset + 4 * i;
            w[i] = (static_cast<std::uint32_t>(bytes[at]) << 24) |
                   (static_cast<std::uint32_t>(bytes[at + 1]) << 16) |
                   (static_cast<std::uint32_t>(bytes[at + 2]) << 8) | bytes[at + 3];
        }
        for (std::size_t i = 16; i < 64; ++i) {
            const auto s0 = Rotate(w[i - 15], 7) ^ Rotate(w[i - 15], 18) ^ (w[i - 15] >> 3);
            const auto s1 = Rotate(w[i - 2], 17) ^ Rotate(w[i - 2], 19) ^ (w[i - 2] >> 10);
            w[i] = w[i - 16] + s0 + w[i - 7] + s1;
        }
        auto a=h[0], b=h[1], c=h[2], d=h[3], e=h[4], f=h[5], g=h[6], z=h[7];
        for (std::size_t i = 0; i < 64; ++i) {
            const auto s1 = Rotate(e, 6) ^ Rotate(e, 11) ^ Rotate(e, 25);
            const auto choose = (e & f) ^ (~e & g);
            const auto t1 = z + s1 + choose + kRound[i] + w[i];
            const auto s0 = Rotate(a, 2) ^ Rotate(a, 13) ^ Rotate(a, 22);
            const auto majority = (a & b) ^ (a & c) ^ (b & c);
            const auto t2 = s0 + majority;
            z=g; g=f; f=e; e=d+t1; d=c; c=b; b=a; a=t1+t2;
        }
        h[0]+=a; h[1]+=b; h[2]+=c; h[3]+=d;
        h[4]+=e; h[5]+=f; h[6]+=g; h[7]+=z;
    }
    std::array<std::uint8_t, 32> digest{};
    for (std::size_t i = 0; i < h.size(); ++i)
        for (int shift = 24; shift >= 0; shift -= 8)
            digest[4 * i + (24 - shift) / 8] = static_cast<std::uint8_t>(h[i] >> shift);
    return digest;
}

void ValidateMap(const FeatureMap& features) {
    for (const auto& [key, pages] : features) {
        if (key.first != "FILE" && key.first != "ANON")
            throw std::invalid_argument("feature namespace must be FILE or ANON");
        if (!std::isfinite(pages)) throw std::invalid_argument("non-finite feature pages");
    }
}

std::vector<double> HashAndNormalize(const FeatureMap& excess) {
    ValidateMap(excess);
    std::vector<double> vector(kVectorDimension, 0.0);
    for (const auto& [key, pages] : excess) {
        // Both frozen Python implementations use log1p(max(pages, 0)).
        vector[StableHashIndex(key.first, key.second)] += std::log1p(std::max(pages, 0.0));
    }
    double square_sum = 0.0;
    for (double value : vector) square_sum += value * value;
    const double norm = std::sqrt(square_sum);
    if (norm > 0.0) for (double& value : vector) value /= norm;
    return vector;  // Python feature construction also preserves zero vectors.
}

double Median(std::vector<double> values) {
    std::sort(values.begin(), values.end());
    const std::size_t middle = values.size() / 2;
    return values.size() % 2 ? values[middle] : (values[middle - 1] + values[middle]) / 2;
}

}  // namespace

std::size_t StableHashIndex(const std::string& name_space,
                            const std::string& feature_key) {
    if (name_space != "FILE" && name_space != "ANON")
        throw std::invalid_argument("unsupported feature namespace");
    const auto digest = Sha256(name_space + ":" + feature_key);
    std::uint64_t first = 0;
    for (std::size_t i = 0; i < 8; ++i) first = (first << 8) | digest[i];
    return (name_space == "FILE" ? 0 : 1024) + first % 1024;
}

std::string Sha256Hex(const std::string& bytes) {
    constexpr char hex[] = "0123456789abcdef";
    const auto digest = Sha256(bytes);
    std::string result;
    result.reserve(64);
    for (const auto byte : digest) {
        result.push_back(hex[byte >> 4]);
        result.push_back(hex[byte & 15]);
    }
    return result;
}

std::vector<double> BuildWpsVector(const FeatureMap& excess_pages) {
    return HashAndNormalize(excess_pages);
}

std::vector<double> BuildCrossAppV2Vector(const std::vector<FeatureMap>& baselines,
                                           const FeatureMap& action,
                                           const FeatureMap& post_action) {
    for (const auto& map : baselines) ValidateMap(map);
    ValidateMap(action);
    ValidateMap(post_action);
    FeatureMap keys = action;
    keys.insert(post_action.begin(), post_action.end());
    FeatureMap excess;
    for (const auto& [key, ignored] : keys) {
        (void)ignored;
        std::vector<double> baseline_values;
        baseline_values.reserve(baselines.size());
        for (const auto& baseline : baselines) {
            const auto found = baseline.find(key);
            baseline_values.push_back(found == baseline.end() ? 0.0 : found->second);
        }
        const double base = baseline_values.empty() ? 0.0 : Median(std::move(baseline_values));
        const auto action_it = action.find(key), post_it = post_action.find(key);
        const double action_value = action_it == action.end() ? 0.0 : action_it->second;
        const double post_value = post_it == post_action.end() ? 0.0 : post_it->second;
        const double value = std::max(action_value, post_value) - base;
        if (value > 0.0) excess.emplace(key, value);
    }
    return HashAndNormalize(excess);
}

}  // namespace harmony_ops
