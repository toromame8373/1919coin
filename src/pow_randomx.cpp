// RandomX proof-of-work glue (verification in light mode, mining in fast mode).
// Distributed under the MIT software license, see the accompanying
// file COPYING or http://www.opensource.org/licenses/mit-license.php.

#include <pow_randomx.h>

#include <crypto/common.h>
#include <primitives/block.h>
#include <randomx.h>
#include <serialize.h>
#include <streams.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <thread>
#include <vector>

namespace randomxpow {
namespace {

// Fixed network key. Changing it forks the chain.
constexpr char RANDOMX_KEY[] = "yajuucoin-randomx-v1";
constexpr size_t RANDOMX_KEY_LEN = sizeof(RANDOMX_KEY) - 1;

constexpr size_t HEADER_SIZE = 80;
constexpr size_t NONCE_OFFSET = 76;

std::once_flag g_cache_once;
randomx_cache* g_cache{nullptr};
randomx_flags g_flags{RANDOMX_FLAG_DEFAULT};

std::once_flag g_dataset_once;
randomx_dataset* g_dataset{nullptr};

// Cache is allocated once and intentionally never freed: threads may still be
// hashing during static destruction.
randomx_cache* GetCache()
{
    std::call_once(g_cache_once, [] {
        g_flags = randomx_get_flags();
        g_cache = randomx_alloc_cache(g_flags);
        if (!g_cache) {
            // e.g. JIT unavailable: fall back to the portable interpreter
            g_flags = RANDOMX_FLAG_DEFAULT;
            g_cache = randomx_alloc_cache(g_flags);
        }
        if (!g_cache) throw std::runtime_error("RandomX: failed to allocate cache");
        randomx_init_cache(g_cache, RANDOMX_KEY, RANDOMX_KEY_LEN);
    });
    return g_cache;
}

randomx_vm* CreateLightVM()
{
    randomx_cache* cache = GetCache();
    randomx_vm* vm = randomx_create_vm(g_flags, cache, nullptr);
    if (!vm) vm = randomx_create_vm(RANDOMX_FLAG_DEFAULT, cache, nullptr);
    if (!vm) throw std::runtime_error("RandomX: failed to create VM");
    return vm;
}

struct VMHolder {
    randomx_vm* vm{nullptr};
    ~VMHolder()
    {
        if (vm) randomx_destroy_vm(vm);
    }
};

std::array<unsigned char, HEADER_SIZE> SerializeHeader(const CBlockHeader& header)
{
    DataStream ss{};
    ss << header;
    if (ss.size() != HEADER_SIZE) throw std::runtime_error("RandomX: unexpected header size");
    std::array<unsigned char, HEADER_SIZE> out;
    std::memcpy(out.data(), ss.data(), HEADER_SIZE);
    return out;
}

// Returns nullptr if the dataset could not be allocated.
randomx_dataset* GetDataset(unsigned threads)
{
    std::call_once(g_dataset_once, [threads] {
        randomx_cache* cache = GetCache();
        randomx_dataset* ds = randomx_alloc_dataset(RANDOMX_FLAG_DEFAULT);
        if (!ds) return;
        const unsigned long items = randomx_dataset_item_count();
        std::vector<std::thread> pool;
        const unsigned n = std::max(1u, threads);
        for (unsigned i = 0; i < n; ++i) {
            const unsigned long start = items * i / n;
            const unsigned long end = items * (i + 1) / n;
            pool.emplace_back([=] { randomx_init_dataset(ds, cache, start, end - start); });
        }
        for (auto& t : pool) t.join();
        g_dataset = ds;
    });
    return g_dataset;
}

unsigned MineThreads()
{
    unsigned n = std::thread::hardware_concurrency();
    if (const char* env = std::getenv("INMU_MINE_THREADS")) {
        const long v = std::strtol(env, nullptr, 10);
        if (v > 0 && v < 4096) n = static_cast<unsigned>(v);
    }
    return std::max(1u, n);
}

} // namespace

uint256 HeaderHash(const CBlockHeader& header)
{
    thread_local VMHolder holder;
    if (!holder.vm) holder.vm = CreateLightVM();
    const auto buf = SerializeHeader(header);
    uint256 out;
    randomx_calculate_hash(holder.vm, buf.data(), buf.size(), out.begin());
    return out;
}

bool Mine(CBlockHeader& header, const arith_uint256& target, uint64_t& max_tries, const std::function<bool()>& interrupted)
{
    const unsigned threads = MineThreads();
    // Very easy targets (regtest) need only a handful of hashes: skip the 2 GiB dataset.
    const bool need_dataset = !(target > (arith_uint256(1) << 250));
    randomx_dataset* dataset = need_dataset ? GetDataset(threads) : nullptr;

    const uint32_t start_nonce = header.nNonce;
    const uint64_t nonce_space = static_cast<uint64_t>(UINT32_MAX) - start_nonce + 1;
    const uint64_t limit = std::min<uint64_t>(max_tries, nonce_space);
    const auto base = SerializeHeader(header);

    std::atomic<uint64_t> next_index{0};
    std::atomic<uint64_t> tried{0};
    std::atomic<bool> found{false};
    std::atomic<bool> abort{false};
    std::atomic<uint32_t> found_nonce{0};
    constexpr uint64_t CHUNK = 64;

    auto worker = [&]() {
        randomx_vm* vm = nullptr;
        if (dataset) {
            vm = randomx_create_vm(g_flags | RANDOMX_FLAG_FULL_MEM, nullptr, dataset);
            if (!vm) vm = randomx_create_vm(RANDOMX_FLAG_FULL_MEM, nullptr, dataset);
        }
        if (!vm) vm = CreateLightVM();
        std::array<unsigned char, HEADER_SIZE> buf = base;
        uint256 hash;
        while (!found.load(std::memory_order_relaxed) && !abort.load(std::memory_order_relaxed)) {
            if (interrupted && interrupted()) {
                abort = true;
                break;
            }
            const uint64_t first = next_index.fetch_add(CHUNK);
            if (first >= limit) break;
            const uint64_t last = std::min<uint64_t>(first + CHUNK, limit);
            for (uint64_t i = first; i < last; ++i) {
                const uint32_t nonce = static_cast<uint32_t>(start_nonce + i);
                WriteLE32(buf.data() + NONCE_OFFSET, nonce);
                randomx_calculate_hash(vm, buf.data(), buf.size(), hash.begin());
                tried.fetch_add(1, std::memory_order_relaxed);
                if (UintToArith256(hash) <= target) {
                    found_nonce = nonce;
                    found = true;
                    break;
                }
            }
        }
        randomx_destroy_vm(vm);
    };

    std::vector<std::thread> pool;
    for (unsigned i = 1; i < threads; ++i) pool.emplace_back(worker);
    worker();
    for (auto& t : pool) t.join();

    const uint64_t done = tried.load();
    max_tries -= std::min<uint64_t>(max_tries, done);
    if (found) {
        header.nNonce = found_nonce.load();
        return true;
    }
    if (!abort && limit == nonce_space && max_tries > 0) {
        // Whole nonce space exhausted without a solution; caller rolls the block template.
        header.nNonce = UINT32_MAX;
    }
    return false;
}

} // namespace randomxpow
