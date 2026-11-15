// Copyright (c) 2009-2010 Satoshi Nakamoto
// Copyright (c) 2009-2022 The Bitcoin Core developers
// Distributed under the MIT software license, see the accompanying
// file COPYING or http://www.opensource.org/licenses/mit-license.php.

#include <pow.h>

#include <algorithm>

#include <arith_uint256.h>
#include <chain.h>
#include <primitives/block.h>
#include <pow_randomx.h>
#include <uint256.h>
#include <util/check.h>

// LWMA (linearly weighted moving average, zawy12 LWMA-1 style) difficulty
// algorithm: retargets every block using the last LWMA_WINDOW solve times.
static constexpr int64_t LWMA_WINDOW = 60;

unsigned int GetNextWorkRequired(const CBlockIndex* pindexLast, const CBlockHeader *pblock, const Consensus::Params& params)
{
    assert(pindexLast != nullptr);
    const arith_uint256 bnPowLimit = UintToArith256(params.powLimit);

    // regtest: fixed difficulty
    if (params.fPowNoRetargeting) return pindexLast->nBits;

    const int64_t T = params.nPowTargetSpacing;
    const int64_t N = LWMA_WINDOW;
    const int64_t height = pindexLast->nHeight;

    // Not enough history yet: keep the initial (genesis) difficulty.
    if (height < N) return pindexLast->nBits;

    const int64_t k = N * (N + 1) * T / 2;
    const arith_uint256 divisor(static_cast<uint64_t>(N * k));

    int64_t previous_ts = pindexLast->GetAncestor(height - N)->GetBlockTime();
    int64_t weighted_time = 0;
    arith_uint256 sum_target;

    for (int64_t j = 1; j <= N; ++j) {
        const CBlockIndex* cur = pindexLast->GetAncestor(height - N + j);
        assert(cur);
        const int64_t this_ts = (cur->GetBlockTime() > previous_ts) ? cur->GetBlockTime() : previous_ts + 1;
        const int64_t solvetime = std::min<int64_t>(6 * T, this_ts - previous_ts);
        previous_ts = this_ts;
        weighted_time += solvetime * j;
        arith_uint256 target;
        target.SetCompact(cur->nBits);
        sum_target += target / divisor;
    }

    // Guard against extreme speed-ups (difficulty at most 10x per step).
    if (weighted_time < k / 10) weighted_time = k / 10;

    arith_uint256 next_target = sum_target;
    next_target *= arith_uint256(static_cast<uint64_t>(weighted_time));
    if (next_target > bnPowLimit) next_target = bnPowLimit;
    return next_target.GetCompact();
}

unsigned int CalculateNextWorkRequired(const CBlockIndex* pindexLast, int64_t nFirstBlockTime, const Consensus::Params& params)
{
    if (params.fPowNoRetargeting)
        return pindexLast->nBits;

    // Limit adjustment step
    int64_t nActualTimespan = pindexLast->GetBlockTime() - nFirstBlockTime;
    if (nActualTimespan < params.nPowTargetTimespan/4)
        nActualTimespan = params.nPowTargetTimespan/4;
    if (nActualTimespan > params.nPowTargetTimespan*4)
        nActualTimespan = params.nPowTargetTimespan*4;

    // Retarget
    const arith_uint256 bnPowLimit = UintToArith256(params.powLimit);
    arith_uint256 bnNew;

    // Special difficulty rule for Testnet4
    if (params.enforce_BIP94) {
        // Here we use the first block of the difficulty period. This way
        // the real difficulty is always preserved in the first block as
        // it is not allowed to use the min-difficulty exception.
        int nHeightFirst = pindexLast->nHeight - (params.DifficultyAdjustmentInterval()-1);
        const CBlockIndex* pindexFirst = pindexLast->GetAncestor(nHeightFirst);
        bnNew.SetCompact(pindexFirst->nBits);
    } else {
        bnNew.SetCompact(pindexLast->nBits);
    }

    bnNew *= nActualTimespan;
    bnNew /= params.nPowTargetTimespan;

    if (bnNew > bnPowLimit)
        bnNew = bnPowLimit;

    return bnNew.GetCompact();
}

// With per-block LWMA retargeting, only check that the target stays within a
// sane factor (16x) of the previous one and never exceeds the PoW limit.
bool PermittedDifficultyTransition(const Consensus::Params& params, int64_t height, uint32_t old_nbits, uint32_t new_nbits)
{
    if (params.fPowAllowMinDifficultyBlocks) return true;
    if (params.fPowNoRetargeting) return old_nbits == new_nbits;

    const arith_uint256 pow_limit = UintToArith256(params.powLimit);
    arith_uint256 old_target, new_target;
    old_target.SetCompact(old_nbits);
    new_target.SetCompact(new_nbits);
    if (new_target > pow_limit) return false;

    arith_uint256 upper = old_target;
    upper *= arith_uint256(16);
    if (upper > pow_limit) upper = pow_limit;
    if (new_target > upper) return false;

    arith_uint256 scaled_new = new_target;
    scaled_new *= arith_uint256(16);
    if (scaled_new < old_target) return false;
    return true;
}

// Bypasses the actual proof of work check during fuzz testing with a simplified validation checking whether
// the most significant bit of the last byte of the hash is set.
bool CheckProofOfWork(uint256 hash, unsigned int nBits, const Consensus::Params& params)
{
    if (EnableFuzzDeterminism()) return (hash.data()[31] & 0x80) == 0;
    return CheckProofOfWorkImpl(hash, nBits, params);
}

bool CheckProofOfWork(const CBlockHeader& header, const Consensus::Params& params)
{
    if (EnableFuzzDeterminism()) return CheckProofOfWork(header.GetHash(), header.nBits, params);
    // Reject malformed nBits before spending time on RandomX.
    auto bnTarget{DeriveTarget(header.nBits, params.powLimit)};
    if (!bnTarget) return false;
    return UintToArith256(randomxpow::HeaderHash(header)) <= *bnTarget;
}

std::optional<arith_uint256> DeriveTarget(unsigned int nBits, const uint256 pow_limit)
{
    bool fNegative;
    bool fOverflow;
    arith_uint256 bnTarget;

    bnTarget.SetCompact(nBits, &fNegative, &fOverflow);

    // Check range
    if (fNegative || bnTarget == 0 || fOverflow || bnTarget > UintToArith256(pow_limit))
        return {};

    return bnTarget;
}

bool CheckProofOfWorkImpl(uint256 hash, unsigned int nBits, const Consensus::Params& params)
{
    auto bnTarget{DeriveTarget(nBits, params.powLimit)};
    if (!bnTarget) return false;

    // Check proof of work matches claimed amount
    if (UintToArith256(hash) > bnTarget)
        return false;

    return true;
}
