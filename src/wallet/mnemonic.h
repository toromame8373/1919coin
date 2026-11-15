// Copyright (c) 2026 The 1919coin developers
// Distributed under the MIT software license, see the accompanying
// file COPYING or http://www.opensource.org/licenses/mit-license.php.

#ifndef BITCOIN_WALLET_MNEMONIC_H
#define BITCOIN_WALLET_MNEMONIC_H

#include <optional>
#include <string>
#include <vector>

namespace wallet {

/** The official BIP39 English word list (2048 words). */
const std::vector<std::string>& BIP39WordList();

/**
 * Validate a BIP39 mnemonic phrase: the word count must be 12, 15, 18, 21
 * or 24, every word must be in the English word list, and the checksum
 * embedded in the phrase must match. Words may be separated by any
 * whitespace. On failure, returns false and describes the problem in error.
 */
bool ValidateMnemonic(const std::string& mnemonic, std::string& error);

/**
 * Derive the 64-byte BIP39 seed from a mnemonic phrase and passphrase
 * (PBKDF2-HMAC-SHA512 with 2048 iterations and salt "mnemonic" + passphrase).
 * Returns std::nullopt if the phrase is not a valid mnemonic.
 * Note: the phrase and passphrase are expected to be ASCII. Unlike full
 * BIP39, NFKD normalization of non-ASCII input is not performed (the word
 * list itself is plain ASCII).
 */
std::optional<std::vector<unsigned char>> MnemonicToSeed(const std::string& mnemonic, const std::string& passphrase);

/**
 * Convert entropy of 16, 20, 24, 28 or 32 bytes into a BIP39 mnemonic
 * phrase of 12, 15, 18, 21 or 24 words. Returns an empty string if the
 * entropy size is invalid.
 */
std::string EntropyToMnemonic(const std::vector<unsigned char>& entropy);

} // namespace wallet

#endif // BITCOIN_WALLET_MNEMONIC_H
