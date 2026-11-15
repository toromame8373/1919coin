# 1919coin

Bitcoin Core v30.4 のフォークです。PoW を **RandomX** に差し替えた新規チェーンです。

## ビルド（Visual Studio 2026）

必要なもの: Visual Studio 2026（「C++によるデスクトップ開発」）、Git。

    build.bat          # デーモン + CLI（軽い）
    build.bat gui      # Qt GUI(1919coin-qt.exe)付き。初回は vcpkg が Qt をビルドするため 1〜2 時間かかります

- vswhere で最新の VS を見つけ、vcvars64 → vcpkg（無ければ `.vcpkg` に clone）→ CMake(Ninja) → ビルドまで自動です。
- 初回は vcpkg が boost / libevent / sqlite をビルドするため 10〜20 分ほどかかります。
- 成果物: `build\bin\1919coind.exe`, `build\bin\1919coin-cli.exe`（gui の場合は `1919coin-qt.exe` も）
- `build.bat` は並列数を既定 2 に制限しています（MSVC の `C1060: ヒープの領域を使い果たしました` 対策。ninja は全コアで重い wallet 系ファイルを並列コンパイルしてメモリを使い切るため）。それでも出る場合は `build.bat gui 1` で 1 並列にして再実行してください。ninja は終わった分を保持するので途中から再開します。
- IDE で開きたい場合: `cmake --preset yju-vs2026`（CMake 4.2 以上。`Visual Studio 18 2026` ジェネレータ）。

## 使い方

    start-node.bat                      # ノード起動 (P2P 11451 / RPC 11450)
    start-gui.bat                       # GUI版を起動（起動中も mine.bat が使えます）
    start-node.bat -addnode=IP:11451    # 相手ノードを直接指定（予備手段。通常は不要）
    mine.bat [アドレス]                 # ソロマイニング（省略時は wallet "miner" の新規アドレス）

- 接続: DNS シード `seed.1919coin.f5.si` がクライアントに組み込まれているので、通常は起動するだけでネットワークに自動接続します。`-addnode` はシードに繋がらない場合の予備手段です。
- データ置き場: `%LOCALAPPDATA%\1919coin`（Bitcoin のデータとは完全に別）
- 初回のマイニング開始時に RandomX の 2GiB データセットを作ります（1分前後）。メモリ 3GiB 以上推奨。
- スレッド数: `set INMU_MINE_THREADS=4` など。

## チェーン仕様

| 項目 | 値 |
|---|---|
| PoW | RandomX（固定キー `yajuucoin-randomx-v1`、シード更新なし） |
| ブロック間隔 | 600 秒（10 分） |
| 初期報酬 | 10 YJU |
| 半減 | 1,050,000 ブロックごと（約4年） |
| 総発行量 | 21,000,000 YJU |
| 難易度調整 | LWMA（直近60ブロック、毎ブロック） |
| プレマイン | なし（ジェネシスのコインベースは UTXO に入らない） |
| P2P / RPC ポート | 11451 / 11450 |
| マジック | `19 19 81 0a` |
| アドレス | 下の「アドレス」参照 |
| ジェネシス | 2026-11-09 00:00:00 UTC, nBits 0x1f00ffff, nonce 2188 |
| ジェネシスblock id | 978de1b1b53a8626e41105b944fd2620852eb6bd43b77c8bf9ed37bd7c15db84 |
| ジェネシス RandomX hash | e14eb4ab…af000000（軽量/高速モードで一致確認済み） |

## アドレス

Bitcoin と同じ仕組み（秘密鍵 → 公開鍵 → ハッシュ → エンコード）で、接頭辞だけ変えています。

| 種類 | 形式 | 見た目 |
|---|---|---|
| bech32 (P2WPKH, 既定) | HRP `iku` | `iku1q...` |
| bech32m (Taproot) | HRP `iku` | `iku1p...` |
| Legacy P2PKH | base58 prefix 78 | `Y...` |
| P2SH | base58 prefix 20 | `9...` |
| WIF 秘密鍵 | prefix 206 | — |

Bitcoin のアドレスにはそのまま送れず（逆も同様）、取り違えは防げます。`getnewaddress` の既定は `iku1q...` です。

## テストネット

メインネットとは独立したテスト用チェーンです。独自ジェネシスを持ち、難易度が極端に低く設定されているので、どの CPU でもすぐブロックが見つかります。発行ルール（10 YJU / 半減 1,050,000 / LWMA 600 秒間隔）はメインネットと同じです。

    start-node.bat -testnet                    # テストネットでノード起動 (P2P 21451 / RPC 21450)
    start-node.bat -testnet -addnode=IP:21451  # 相手ノードを直接指定（予備手段。通常はDNSシードで自動接続）
    start-gui.bat -testnet                     # GUI でも可

- データ置き場: `%LOCALAPPDATA%\1919coin\testnet3`（メインネットと分離）
- マジック: `19 19 81 0b`（メインネットは `...0a`）
- ジェネシス: 2026-11-15 00:00:00 UTC, nBits 0x207fffff, nonce 1, block id `a60535e438a4c346c1a539fff59eb9c68af87aa89fab696ccf2fd977101b21be`
- アドレス形式: bech32 は HRP `tiku`（`tiku1q...`）、Legacy P2PKH は prefix 90（`d...`）、P2SH は prefix 32（`D...`）、WIF は prefix 218。メインネット・Bitcoin テストネットのどちらとも混同しません。
- テストネットのコインに実価値はありません。

## GUI

`build.bat gui` で Bitcoin Core 標準の Qt GUI（残高・送受信・デバッグコンソール）が付きます。単位表示は `YJU` / `mYJU` / `µYJU` です（CLI/RPC の手数料表記の単位も `YJU`）。アイコンは 1919coin の画像に差し替え済みです（`src/qt/res/icons/`）。

## 主な改造点

- `src/pow_randomx.{h,cpp}` : RandomX 連携。検証は軽量モード(256MiB)、採掘は高速モード(2GiB、確保失敗時は軽量にフォールバック)。
- `src/pow.cpp` : `CheckProofOfWork(const CBlockHeader&)` 追加、LWMA、`PermittedDifficultyTransition` を16倍以内チェックに変更。
- ブロックID（`GetHash()`）は従来どおり SHA256d のまま。PoW 判定だけ RandomX ハッシュを使います。
- `validation.cpp`, `rpc/mining.cpp`（`generatetoaddress` が RandomX 採掘）, `node/blockstorage.cpp`（起動時/読み出し時の再検証を省略）。
- `src/randomx/` : RandomX v1.2.3 を同梱（BSD-3）。
- `kernel/chainparams.cpp` : メインネット/テストネット定義、`common/args.cpp` : データフォルダ名を 1919coin に変更。
- 実行ファイル名: CMake の `OUTPUT_NAME` で 1919coin 系に変更（下記の対応表）。

## 注意

- signet/regtest の PoW は未対応です（テストネットは実装済み。regtest を使う場合は別途調整が必要）。
- ジェネシスのタイムスタンプ文字列は、メインネットが `1919coin - 114514 1919 810 - RandomX genesis 2026-11-09`、テストネットが `1919coin testnet 2026-11-15 - RandomX genesis` です。
- 実行ファイル名は 1919coin 系に変更済みです: `bitcoind` → `1919coind`、`bitcoin-cli` → `1919coin-cli`、`bitcoin-qt` → `1919coin-qt`、`bitcoin-tx` → `1919coin-tx`、`bitcoin-util` → `1919coin-util`、`bitcoin-wallet` → `1919coin-wallet`（IPC 用の `bitcoin-node` / `bitcoin-gui` とラッパー `bitcoin` も同様に `1919coin-node` / `1919coin-gui` / `1919coin`）。
- 51%攻撃に弱い小規模チェーンです。取引所上場や他人への販売をする場合は、法規制・説明責任に注意してください。
- 2026-11-15、Linux (Ubuntu 24.04 / gcc 13.3) でフルビルド（bitcoind / bitcoin-cli）とノード起動・採掘・アドレス生成を当方でも実測確認しました（ジェネシス block id 一致、報酬 10/ブロックを残高で確認）。詳細は配布元の作業ノート NOTES.md 参照。
- 2026-11-15、Windows の MSVC 実機ビルドも成功確認済みです（bitcoind.exe が起動し、ジェネシス block id が一致）。ただし `build.bat gui` の初回は C1060 で止まることがあり、その場合は上記の並列数 1 での再実行で対応してください。
