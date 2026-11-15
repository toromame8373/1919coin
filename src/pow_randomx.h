// RandomX proof-of-work glue (verification in light mode, mining in fast mode).
// Distributed under the MIT software license, see the accompanying
// file COPYING or http://www.opensource.org/licenses/mit-license.php.

#ifndef BITCOIN_POW_RANDOMX_H
#define BITCOIN_POW_RANDOMX_H

#include <arith_uint256.h>
#include <uint256.h>

#include <cstdint>
#include <functional>

class CBlockHeader;

namespace randomxpow {

/**
 * RandomX hash of the serialized 80-byte block header.
 * Uses a fixed network key (no seed rotation), light mode (256 MiB cache),
 * and is safe to call from any thread.
 */
uint256 HeaderHash(const CBlockHeader& header);

/**
 * Multi-threaded nonce search in RandomX fast mode (2 GiB dataset, falls back
 * to light mode if the dataset cannot be allocated).
 *
 * Environment variable INMU_MINE_THREADS selects the thread count
 * (default: all hardware threads).
 *
 * @param[in,out] header     header template; nNonce is the first nonce to try and
 *                           receives the winning nonce on success
 * @param[in]     target     hash must be <= target
 * @param[in,out] max_tries  upper bound on hashes; decremented by hashes done
 * @param[in]     interrupted polled regularly; returns true to abort
 * @return true if a valid nonce was found
 */
bool Mine(CBlockHeader& header, const arith_uint256& target, uint64_t& max_tries, const std::function<bool()>& interrupted);

} // namespace randomxpow

#endif // BITCOIN_POW_RANDOMX_H
