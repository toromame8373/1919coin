#!/usr/bin/env python3
"""run_test.py - end-to-end verification of the 1919coin pool on an
ISOLATED testnet node (-connect=0, no external peers).

Sequence:
  1. fresh testnet node in /tmp/pooltest (wiped first)
  2. wallet + pool/miner addresses
  3. poold (autopay on, testnet) + two poolminer clients
     (miner1 runs the bad-share rejection probes first)
  4. wait until the first pool-found block matures (height >= 101)
     and its payout is executed via sendmany
  5. assertions: rejection probes, share/block accounting consistency,
     payout proportionality vs the block's round shares, payout txid
     present in the node wallet, stats endpoint, state persistence
     across a poold restart
  6. stop everything; copy logs + final state to ./test-evidence/

Mainnet is never touched. Expected runtime: a few minutes.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request

BASE = os.path.expanduser("~/workspace/1919coin")
POOL = os.path.join(BASE, "pool")
WORK = "/tmp/pooltest"
EVID = os.path.join(POOL, "test-evidence")
ENV = dict(os.environ, LD_LIBRARY_PATH="/tmp/libev")
BIN = os.path.join(BASE, "build/bin")
CLI = [os.path.join(BIN, "1919coin-cli"), "-testnet",
       "-datadir=" + os.path.join(WORK, "node")]

procs = []


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, env=ENV, **kw)


def cli(*args, wallet=None):
    cmd = list(CLI)
    if wallet:
        cmd.append("-rpcwallet=" + wallet)
    cmd += list(args)
    r = run(cmd)
    if r.returncode != 0:
        raise RuntimeError("cli %s failed: %s" % (args, r.stderr.strip()))
    return r.stdout.strip()


def spawn(name, cmd, logname):
    logf = open(os.path.join(WORK, logname), "wb")
    p = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT,
                         env=ENV, start_new_session=True,
                         cwd=POOL)
    procs.append((name, p))
    return p


def stop_all():
    for _name, p in procs:
        try:
            os.killpg(p.pid, signal.SIGTERM)
        except OSError:
            pass
    time.sleep(2)
    for _name, p in procs:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass


def load_state():
    with open(os.path.join(WORK, "pool-state.json")) as f:
        return json.load(f)


def main():
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(os.path.join(WORK, "node"))
    os.makedirs(EVID, exist_ok=True)
    results = []

    def check(name, ok, detail=""):
        results.append((name, ok, detail))
        print("%s %s %s" % ("PASS" if ok else "FAIL", name, detail),
              flush=True)

    # 1. node
    spawn("node", [os.path.join(BIN, "1919coind"), "-testnet",
                   "-datadir=" + os.path.join(WORK, "node"),
                   "-connect=0", "-listen=0", "-dnsseed=0",
                   "-fallbackfee=0.0002", "-daemon"], "node.log")
    for _ in range(60):
        try:
            if cli("getblockcount") == "0":
                break
        except Exception:
            time.sleep(1)
    info = json.loads(cli("getblockchaininfo"))
    check("isolated testnet node up", info["chain"] == "test"
          and info["blocks"] == 0, info["bestblockhash"][:16])

    # 2. wallet + addresses
    cli("createwallet", "pooltest")
    pool_addr = cli("getnewaddress", "pool", wallet="pooltest")
    m1 = cli("getnewaddress", "miner1", wallet="pooltest")
    m2 = cli("getnewaddress", "miner2", wallet="pooltest")
    check("testnet addresses", all(a.startswith("tiku1") for a in
                                   (pool_addr, m1, m2)), pool_addr)

    # 3. pool + miners
    spawn("poold", [sys.executable, "poold.py",
                    "--pool-address", pool_addr, "--chain-hint", "test",
                    "--rpc-datadir", os.path.join(WORK, "node/testnet3"),
                    "--rpc-wallet", "pooltest", "--autopay",
                    "--share-target-hex",
                    "c000000000000000000000000000000000000000000000000000000000000000",
                    "--state", os.path.join(WORK, "pool-state.json")],
          "poold.log")
    time.sleep(6)
    spawn("miner1", [sys.executable, "poolminer.py", "--address", m1,
                     "--helpers", "2", "--bad-share-test"], "miner1.log")
    time.sleep(2)
    spawn("miner2", [sys.executable, "poolminer.py", "--address", m2,
                     "--helpers", "2", "--en2-start", "500000"],
          "miner2.log")

    # 4. wait for first payout (block 1 matures at height 101)
    deadline = time.time() + 900
    paid_block = None
    while time.time() < deadline:
        time.sleep(5)
        try:
            st = load_state()
        except Exception:
            continue
        if st["payouts"]:
            paid_block = st["blocks"][0]
            break
        if int(cli("getblockcount")) % 25 == 0:
            print("  ... height=%s shares=%s blocks=%s"
                  % (cli("getblockcount"), st["total_shares"],
                     len(st["blocks"])), flush=True)
    check("first block matured and payout executed", paid_block is not None)

    st = load_state()
    height = int(cli("getblockcount"))

    # 5. assertions
    m1log = open(os.path.join(WORK, "miner1.log")).read()
    check("bad share rejected (low difficulty)",
          "[low difficulty] -> low difficulty share" in m1log)
    check("bad share rejected (duplicate)",
          "[duplicate] -> duplicate share" in m1log)
    check("bad share rejected (unknown job)",
          "[unknown job] -> job not found" in m1log)
    check("invalid shares counted", st["total_invalid"] >= 1,
          "invalid=%d" % st["total_invalid"])
    check("all chain blocks came via the pool",
          len(st["blocks"]) == height,
          "pool blocks=%d chain height=%d" % (len(st["blocks"]), height))
    per_miner_blocks = sum(m["blocks"] for m in st["miners"].values())
    check("per-miner block counts consistent",
          per_miner_blocks == len(st["blocks"]),
          "sum=%d" % per_miner_blocks)
    check("both miners have shares",
          all(st["miners"][a]["shares"] > 0 for a in (m1, m2)))

    pay = st["payouts"][0]
    rs = paid_block["round_shares"]
    total_rs = sum(rs.values())
    reward = paid_block["reward_sats"]
    expect = {a: reward * c // total_rs for a, c in rs.items()}
    got = pay["amounts_sats"]
    check("payout amounts proportional to round shares",
          got == {a: v for a, v in expect.items() if v >= 546},
          "expect=%s got=%s" % (expect, got))
    check("payout mode is autopay with txid",
          pay["mode"] == "autopay" and bool(pay["txid"]),
          "txid=%s" % pay["txid"])
    if pay["txid"]:
        tx = json.loads(cli("gettransaction", pay["txid"],
                            wallet="pooltest"))
        check("payout tx present in node wallet",
              tx["txid"] == pay["txid"]
              and tx.get("confirmations", 0) >= 0)
        bal = json.loads(cli("getbalances", wallet="pooltest"))
        check("pool wallet balance reflects payout",
              bal["mine"]["trusted"] > 0, json.dumps(bal["mine"]))

    # Freeze share flow before comparing stats vs ledger (both keep
    # moving while miners are connected, which is a test-side race).
    for name, p in procs:
        if name.startswith("miner"):
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except OSError:
                pass
    time.sleep(4)
    st = load_state()
    stats = json.loads(urllib.request.urlopen(
        "http://127.0.0.1:18080/stats.json", timeout=10).read())
    check("stats endpoint", stats["blocks_found"] == len(st["blocks"])
          and stats["connected_miners"] == 0
          and stats["total_shares"] == st["total_shares"],
          "blocks=%s shares=%s" % (stats["blocks_found"],
                                   stats["total_shares"]))

    # state persistence across a poold restart
    for name, p in procs:
        if name == "poold":
            os.killpg(p.pid, signal.SIGTERM)
    time.sleep(3)
    spawn("poold2", [sys.executable, "poold.py",
                     "--pool-address", pool_addr, "--chain-hint", "test",
                     "--rpc-datadir", os.path.join(WORK, "node/testnet3"),
                     "--rpc-wallet", "pooltest", "--autopay",
                     "--share-target-hex",
                     "c000000000000000000000000000000000000000000000000000000000000000",
                     "--state", os.path.join(WORK, "pool-state.json")],
          "poold2.log")
    time.sleep(8)
    stats2 = json.loads(urllib.request.urlopen(
        "http://127.0.0.1:18080/stats.json", timeout=10).read())
    check("state survives poold restart",
          stats2["total_shares"] == stats["total_shares"]
          and stats2["blocks_found"] == stats["blocks_found"],
          "shares=%s blocks=%s" % (stats2["total_shares"],
                                   stats2["blocks_found"]))

    # 6. evidence + shutdown
    for f in ("poold.log", "miner1.log", "miner2.log", "pool-state.json"):
        shutil.copy(os.path.join(WORK, f), os.path.join(EVID, f))
    stop_all()
    try:
        cli("stop")
    except Exception:
        pass
    fails = [r for r in results if not r[1]]
    print("\n==== %d/%d checks passed ====" % (len(results) - len(fails),
                                               len(results)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
