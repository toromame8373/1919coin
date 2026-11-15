# 1919coin マイニングプール v1

Bitcoin Core v30.4 フォーク「1919coin」（RandomX PoW）用のマイニングプールです。

## XMRig 互換について（結論: 素の XMRig は使えない）

XMRig の rx/0 stratum はこのフォークと互換ではありません。理由は3点あります。

1. **RandomX キーの形式**: stratum の `seed_hash` は32バイト固定で、そのまま
   RandomX のキーとして使われます。本フォークのキーは固定 ASCII 文字列
   `yajuucoin-randomx-v1`（20バイト）で、`randomx_init_cache` の初期化は
   キーの長さを含む入力に依存するため、32バイトの seed では同一の
   キャッシュを再現できません（XMRig の `Job::setSeedHash` も32バイト
   以外を受け付けません）。
2. **ジョブの形式**: XMRig は Monero 形式の blockhashing blob を採掘し、
   nonce をオフセット39に書き込みます。本フォークの PoW 入力は80バイトの
   Bitcoin ブロックヘッダで、nonce はオフセット76です。
3. **合否判定**: XMRig は RandomX 出力の上位64bit と64bit の target を
   比較しますが、コンセンサスは256bit フル比較（`hash <= target`）です。

そのため本プールは専用プロトコル（下記）を定義し、専用マイナー
（`poolminer.py`）を同梱します。XMRig 本体を改造する路線の評価は
`../xmrig/README-1919coin-xmrig.md` を参照してください。

## 構成

| ファイル | 役割 |
|---|---|
| `poold.py` | プール本体。Python 標準ライブラリのみ |
| `poolminer.py` | プール専用マイナー（テスト用クライアント） |
| `rxhelper.cpp` / `Makefile` | RandomX ハッシュ計算ヘルパー（ノード同梱の librandomx をリンク） |
| `run_test.py` | 隔離テストネットでの一括検証ドライバ |
| `test-evidence/` | 検証時のログと台帳スナップショット |

プールは隣にフルノード（1919coind）を必要とし、JSON-RPC で連携します。

- `getblocktemplate` でジョブ生成、`submitblock` で発見ブロックを提出
- `validateaddress` でログインアドレスとプールアドレスを検証
- `sendmany` で支払い（**テストネットのみ自動実行**。下記参照）

share の RandomX 検証は `rxhelper` サブプロセス（light モード、256MiB
キャッシュ）が常駐して行います。PoW 入力は80バイトのブロックヘッダ全体、
合否は RandomX 出力32バイトを little-endian の256bit 整数として target と
比較します（ノードの `CheckProofOfWork` と同一）。

## プロトコル（JSON Lines over TCP）

1行1 JSON、改行区切りです。バイナリ項目は hex 文字列で、断りのない限り
**ワイヤ順**（シリアライズされたヘッダ/トランザクションに現れるバイト順）
です。target 類だけは通常の big-endian 16進表記です。

### login（マイナー → プール）

```json
{"id":1,"method":"login","params":{"login":"tiku1q...","agent":"poolminer/1.0"}}
```

- `login` は支払いアドレス。`アドレス.ワーカー名` の形式も可。
- アドレスはノードの `validateaddress` で検証され、無効なら拒否されます。
- 応答で `extranonce1`（接続ごとの4バイト）が返り、続いて job が届きます。

### job（プール → マイナー、通知）

```json
{"id":null,"method":"job","params":{
  "job_id":"1a","height":123,
  "version":536870912,"prevhash":"<64hex ワイヤ順>",
  "coinbase1":"<hex>","coinbase2":"<hex>",
  "merkle_branches":["<64hex>", ...],
  "time":1700000000,"bits":"207fffff",
  "share_target":"<64hex>","block_target":"<64hex>",
  "extranonce1":"01000000","extranonce2_size":4}}
```

- coinbase（stripped 形式）= `coinbase1 || extranonce2(4B) || coinbase2`。
  coinbase1 は extranonce1 の直後まで、coinbase2 は sequence からです。
- マークルルート: `h = dsha256(coinbase)`、各 branch について
  `h = dsha256(h || branch)`。
- ヘッダ = `version(LE32) || prevhash || merkle_root || time(LE32) ||
  bits(LE32) || nonce(LE32)` の80バイト。nonce はオフセット76。
- マイナーは extranonce2 と nonce を回し、`RandomX(ヘッダ) <= share_target`
  を満たすものを提出します。時刻（time）はプールが固定します（v1 では
  マイナーは回しません）。

### submit（マイナー → プール）

```json
{"id":2,"method":"submit","params":{"job_id":"1a","extranonce2":"00000000","nonce":"00000000"}}
```

- `nonce` / `extranonce2` はヘッダ/coinbase に入る4バイトそのものの hex。
- 応答: 受理時は `{"result":{"status":"accepted","block":false,...}}`、
  拒否時は `{"error":"low difficulty share" | "duplicate share" |
  "job not found" | ...}`。
- ブロック target も満たす share はプールがブロックを組み立てて
  `submitblock` し、受理が確認できたものだけを「発見ブロック」として
  台帳に記録します。

### getjob（マイナー → プール）

現在の job を再送します（応答とは別に job 通知が届きます）。

## 支払い方式

- コインベースは全額プールアドレスに入ります。
- share は「ラウンド」で集計します。プールがブロックを発見するまでの
  share が、そのブロックのラウンドに属します。
- ブロックが成熟（100ブロック経過）したら、手数料（`--pool-fee-percent`、
  既定0%）を引いた報酬を、そのラウンドの share 比率で按分します。
  546 sat 未満の端数は dust として台帳に記録し送金しません。
- **自動送金（sendmany）はテストネット限定**です。ノードが報告する
  chain が `test` で `--autopay` が付いている場合だけ実行します。
  メインネットでは計算と台帳記録までで、送金はしません（台帳の
  payouts に `mode: "manual"` として残ります）。
- 台帳は `--state` の JSON ファイルに原子的に（tmp+rename）保存され、
  再起動しても share 数・発見ブロック・支払い記録は消えません。

## 起動方法

前提: ノードをビルド済み（`../build/bin/1919coind` と
`../build/src/randomx/librandomx.a`）で、`make` で rxhelper を
ビルドしておくこと。この VM では 1919coind の起動に libevent の
.so.7（extra/pthreads）が要る場合があります（環境リセットで消える）。

```sh
# ノード（テストネットの例。本番は隣に常駐ノードを用意する）
1919coind -testnet -datadir=<dir> -daemon -fallbackfee=0.0002
1919coin-cli -testnet -datadir=<dir> createwallet pool

# プール（テストネット）
python3 poold.py \
  --pool-address <プール用アドレス> \
  --chain-hint test \
  --rpc-datadir <dir>/testnet3 \
  --rpc-wallet pool \
  --autopay

# マイナー
python3 poolminer.py --address <自分のアドレス> --host <プールのIP>
```

主なオプション（poold.py）:

- `--port 13333` マイナー接続ポート、`--http-port 18080` 統計ポート
- `--share-multiplier N`: share target = ブロック target × N（既定4）。
  `--share-target-hex` で直接指定も可
- `--pool-fee-percent N`: プール手数料（既定0）
- `--rpc-user/--rpc-password`: cookie の代わりの RPC 認証
- 統計: `GET http://<host>:18080/stats.json`（接続中マイナー数、
  推定ハッシュレート、share 数、発見ブロック、支払い記録）

## 本番運用で必要なもの

- **常駐フルノードが隣に必須**です（GBT/submitblock の相手）。
  プール単体では動きません。
- プールアドレスのウォレットをそのノードに置くこと（支払いの原資は
  成熟したコインベースです）。メインネットでは自動送金しないので、
  台帳の payouts を見て手動送金する運用になります。
- share target は実ネットワークの難易度に合わせて調整してください
  （テストネットの易しい target 前提の値は使えません）。

## 検証

`python3 run_test.py` が隔離テストネット（`-connect=0`）で一括検証を
行います。2026-11-15 の実行で 16/16 項目 PASS（詳細は `../NOTES.md`
のプールの節と `test-evidence/`）:

- 正しい share の受理、壊れた share（低難易度・重複・未知ジョブ）の拒否
- ブロック発見 → submitblock → チェーン高さ増加（全ブロックがプール経由）
- 成熟後の比例配分と sendmany 実行（txid をウォレットで確認）
- 台帳の永続化（poold 再起動後も集計一致）
