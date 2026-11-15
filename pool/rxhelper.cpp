// rxhelper - persistent RandomX hashing helper for the 1919coin pool.
//
// Speaks a trivial line protocol on stdin/stdout:
//   in : one line with 160 hex chars (the 80-byte block header)
//   out: one line with 64 hex chars (the 32-byte RandomX hash, raw order)
//
// The RandomX key is the fork's fixed network key, identical to
// src/pow_randomx.cpp in the node: "yajuucoin-randomx-v1" (20 bytes,
// passed verbatim to randomx_init_cache). Changing it forks the chain.
//
// Modes:
//   (default)  light mode: 256 MiB cache only. Used for share verification.
//   --fast     fast mode: full 2 GiB dataset. Used by the test miner.
//
// Build: see Makefile (links the node's bundled librandomx.a).

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <randomx.h>

static const char* RANDOMX_KEY = "yajuucoin-randomx-v1";

static int hexval(char c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

int main(int argc, char** argv)
{
    bool fast = false;
    for (int i = 1; i < argc; ++i) {
        if (std::strcmp(argv[i], "--fast") == 0) fast = true;
    }

    randomx_flags flags = randomx_get_flags();
    randomx_cache* cache = randomx_alloc_cache(flags);
    if (!cache) {
        flags = RANDOMX_FLAG_DEFAULT;
        cache = randomx_alloc_cache(flags);
    }
    if (!cache) { std::fprintf(stderr, "rxhelper: alloc_cache failed\n"); return 1; }
    randomx_init_cache(cache, RANDOMX_KEY, std::strlen(RANDOMX_KEY));

    randomx_dataset* dataset = nullptr;
    if (fast) {
        dataset = randomx_alloc_dataset(RANDOMX_FLAG_DEFAULT);
        if (dataset) {
            const unsigned long items = randomx_dataset_item_count();
            // Single-threaded init is slow but simple; the pool test miner
            // is the only fast-mode user and starts once.
            randomx_init_dataset(dataset, cache, 0, items);
        } else {
            std::fprintf(stderr, "rxhelper: alloc_dataset failed, light mode\n");
        }
    }

    randomx_vm* vm = nullptr;
    if (dataset) {
        vm = randomx_create_vm(flags | RANDOMX_FLAG_FULL_MEM, nullptr, dataset);
        if (!vm) vm = randomx_create_vm(RANDOMX_FLAG_FULL_MEM, nullptr, dataset);
    }
    if (!vm) vm = randomx_create_vm(flags, cache, nullptr);
    if (!vm) vm = randomx_create_vm(RANDOMX_FLAG_DEFAULT, cache, nullptr);
    if (!vm) { std::fprintf(stderr, "rxhelper: create_vm failed\n"); return 1; }

    // Ready marker on stderr so the parent can wait for cache init.
    std::fprintf(stderr, "rxhelper: ready (%s mode)\n", dataset ? "fast" : "light");
    std::fflush(stderr);

    char line[512];
    unsigned char header[80];
    unsigned char hash[32];
    static const char* HEX = "0123456789abcdef";
    while (std::fgets(line, sizeof(line), stdin)) {
        size_t len = std::strlen(line);
        while (len && (line[len - 1] == '\n' || line[len - 1] == '\r')) line[--len] = 0;
        if (len == 0) continue;
        if (len != 160) { std::printf("ERR bad length\n"); std::fflush(stdout); continue; }
        bool ok = true;
        for (int i = 0; i < 80; ++i) {
            int hi = hexval(line[2 * i]), lo = hexval(line[2 * i + 1]);
            if (hi < 0 || lo < 0) { ok = false; break; }
            header[i] = (unsigned char)(hi * 16 + lo);
        }
        if (!ok) { std::printf("ERR bad hex\n"); std::fflush(stdout); continue; }
        randomx_calculate_hash(vm, header, sizeof(header), hash);
        char out[65];
        for (int i = 0; i < 32; ++i) { out[2 * i] = HEX[hash[i] >> 4]; out[2 * i + 1] = HEX[hash[i] & 15]; }
        out[64] = 0;
        std::printf("%s\n", out);
        std::fflush(stdout);
    }
    randomx_destroy_vm(vm);
    return 0;
}
