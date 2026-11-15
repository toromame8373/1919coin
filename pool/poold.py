#!/usr/bin/env python3
"""poold.py - mining pool server v1 for 1919coin (Bitcoin Core v30.4 fork,
RandomX PoW).

Design
------
* Talks to a neighbouring 1919coind over JSON-RPC:
  getblocktemplate (jobs), submitblock (found blocks), getblockcount /
  getbestblockhash (tip watch), validateaddress (login + payout scripts),
  sendmany (payouts, TESTNET ONLY - see below).
* Miners speak a small stratum-like protocol: JSON Lines over TCP
  (see README.md "Protocol"). Stock XMRig rx/0 stratum is NOT compatible
  with this fork (fixed 20-byte ASCII RandomX key vs 32-byte seed_hash,
  Monero blob/nonce offset 39 vs 80-byte Bitcoin header/nonce offset 76,
  64-bit target check vs full 256-bit target check), so a dedicated
  miner (poolminer.py) is bundled instead.
* Share verification uses RandomX in light mode via persistent rxhelper
  subprocesses (the node's bundled librandomx). PoW input is the full
  80-byte serialized block header; the hash (raw RandomX output bytes,
  interpreted little-endian as a 256-bit integer) must be <= target,
  exactly like CheckProofOfWork in src/pow.cpp.
* The coinbase pays the pool address. Shares are accounted per payout
  "round": shares submitted between two pool-found blocks belong to the
  later block's round. When a found block matures (COINBASE_MATURITY =
  100 blocks), the reward is split proportionally to the round's shares.
* Payout execution is hard-guarded: sendmany runs ONLY when the node
  reports chain == "test" AND --autopay is on. On mainnet the pool only
  computes payouts and records them in the ledger as "manual" entries.
* State is persisted as JSON (atomic tmp+rename) so restarts lose
  nothing: per-address shares / invalid shares / blocks, found blocks
  with their rounds, payout records.

Python standard library only (plus the rxhelper binary for RandomX).
"""

import argparse
import hashlib
import http.client
import json
import os
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

COIN = 100_000_000
DUST = 546
MATURITY = 100
MAX_TARGET = (1 << 256) - 1


def dsha256(b):
    return hashlib.sha256(hashlib.sha256(b).digest()).digest()


def now():
    return int(time.time())


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def log(msg):
    print("[%s] %s" % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), msg),
          flush=True)


# ------------------------------------------------------------------ varint / script helpers

def encode_varint(n):
    if n < 0xfd:
        return struct.pack("<B", n)
    if n <= 0xffff:
        return b"\xfd" + struct.pack("<H", n)
    if n <= 0xffffffff:
        return b"\xfe" + struct.pack("<I", n)
    return b"\xff" + struct.pack("<Q", n)


def script_num_push(n):
    """CScript push of an integer (BIP34 height), mirroring miner.cpp."""
    if 1 <= n <= 16:
        return bytes([0x50 + n])
    # script number: little-endian signed magnitude
    if n == 0:
        enc = b""
    else:
        neg = n < 0
        v = abs(n)
        out = bytearray()
        while v:
            out.append(v & 0xff)
            v >>= 8
        if out[-1] & 0x80:
            out.append(0x80 if neg else 0x00)
        elif neg:
            out[-1] |= 0x80
        enc = bytes(out)
    if len(enc) < 0x4c:
        return bytes([len(enc)]) + enc
    return b"\x4c" + bytes([len(enc)]) + enc


# ------------------------------------------------------------------ RPC client

class RpcError(Exception):
    pass


class Rpc:
    """Minimal JSON-RPC client for 1919coind (cookie or user/pass auth)."""

    def __init__(self, host, port, datadir=None, user=None, password=None,
                 wallet=None, timeout=30):
        self.host = host
        self.port = port
        self.datadir = datadir
        self.user = user
        self.password = password
        self.wallet = wallet
        self.timeout = timeout
        self._id = 0
        self._lock = threading.Lock()

    def _auth(self):
        if self.user is not None:
            return "%s:%s" % (self.user, self.password or "")
        # cookie file: <datadir>/.cookie or <datadir>/<subdir>/.cookie
        for cand in self._cookie_paths():
            try:
                with open(cand, "r") as f:
                    return f.read().strip()
            except OSError:
                continue
        raise RpcError("no RPC credentials (cookie not found)")

    def _cookie_paths(self):
        if not self.datadir:
            return []
        return [os.path.join(self.datadir, ".cookie")]

    def call(self, method, params=None, wallet=None):
        import base64
        with self._lock:
            self._id += 1
            rid = self._id
        body = json.dumps({"jsonrpc": "1.0", "id": rid, "method": method,
                           "params": params or []}).encode()
        path = "/"
        w = wallet if wallet is not None else self.wallet
        if w:
            path = "/wallet/" + w
        headers = {"Content-Type": "application/json",
                   "Authorization": "Basic " + base64.b64encode(
                       self._auth().encode()).decode()}
        conn = http.client.HTTPConnection(self.host, self.port,
                                          timeout=self.timeout)
        try:
            conn.request("POST", path, body, headers)
            resp = conn.getresponse()
            data = resp.read()
        finally:
            conn.close()
        try:
            obj = json.loads(data.decode())
        except Exception:
            raise RpcError("%s: non-JSON response (HTTP %s)" % (method, resp.status))
        if obj.get("error"):
            raise RpcError("%s: %s" % (method, obj["error"]))
        return obj.get("result")


# ------------------------------------------------------------------ RandomX verifier (rxhelper subprocesses)

class RxHelperPool:
    """Round-robin pool of persistent rxhelper processes (light mode)."""

    def __init__(self, binary, count=2, fast=False):
        self.binary = binary
        self.count = count
        self.fast = fast
        self.helpers = []  # list of [proc, lock]
        self._rr = 0
        self._rr_lock = threading.Lock()
        for _ in range(count):
            self.helpers.append([self._spawn(), threading.Lock()])

    def _spawn(self):
        args = [self.binary] + (["--fast"] if self.fast else [])
        proc = subprocess.Popen(args, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL)
        return proc

    def hash(self, header80):
        """Return the 32-byte RandomX hash (raw order) of an 80-byte header."""
        assert len(header80) == 80
        line = header80.hex().encode() + b"\n"
        for _attempt in range(2):
            with self._rr_lock:
                idx = self._rr % len(self.helpers)
                self._rr += 1
            proc, lock = self.helpers[idx]
            with lock:
                try:
                    proc.stdin.write(line)
                    proc.stdin.flush()
                    out = proc.stdout.readline().strip()
                except (BrokenPipeError, OSError):
                    out = b""
                if len(out) == 64:
                    return bytes.fromhex(out.decode())
                # helper died; respawn once and retry
                try:
                    proc.kill()
                except OSError:
                    pass
                self.helpers[idx][0] = self._spawn()
        raise RuntimeError("rxhelper unavailable")


# ------------------------------------------------------------------ block template / job

class Template:
    def __init__(self, gbt, pool_script):
        self.version = gbt["version"]
        self.prevhash_disp = gbt["previousblockhash"]
        self.prevhash = bytes.fromhex(self.prevhash_disp)[::-1]  # internal order
        self.coinbasevalue = gbt["coinbasevalue"]
        self.target = int(gbt["target"], 16)
        self.bits = int(gbt["bits"], 16)
        self.curtime = gbt["curtime"]
        self.height = gbt["height"]
        self.witness_commitment = gbt.get("default_witness_commitment")
        self.txs = []  # list of (data_bytes, txid_internal)
        for tx in gbt.get("transactions", []):
            self.txs.append((bytes.fromhex(tx["data"]),
                             bytes.fromhex(tx["txid"])[::-1]))
        self.pool_script = pool_script

    # Coinbase layout mirrors miner.cpp BuildBlock (verified against the
    # real node): scriptSig = height push + OP_0 + extranonce1(4) +
    # extranonce2(4); one output to the pool script; optional 0-value
    # witness-commitment output; locktime = height-1; tx version 2.
    def _coinbase_parts(self, en1, full):
        height_push = script_num_push(self.height)
        scriptsig_head = height_push + b"\x00" + struct.pack("<I", en1)
        # scriptSig total = head + en2(4)
        scriptsig_len = len(scriptsig_head) + 4
        vin = (b"\x00" * 32 + b"\xff\xff\xff\xff" +
               encode_varint(scriptsig_len) + scriptsig_head)
        # coinbase1 ends right after extranonce1; the miner appends en2,
        # then coinbase2 (starting with the sequence bytes).
        coinbase1 = struct.pack("<I", 2)  # tx version
        if full and self.witness_commitment:
            coinbase1 += b"\x00\x01"  # segwit marker/flag (full form only)
        coinbase1 += encode_varint(1) + vin
        vout = b""
        vout += struct.pack("<q", self.coinbasevalue)
        vout += encode_varint(len(self.pool_script)) + self.pool_script
        nout = 1
        if self.witness_commitment:
            commitment = bytes.fromhex(self.witness_commitment)
            vout += struct.pack("<q", 0)
            vout += encode_varint(len(commitment)) + commitment
            nout = 2
        coinbase2 = (b"\xfe\xff\xff\xff" +  # sequence 0xfffffffe
                     encode_varint(nout) + vout)
        if full and self.witness_commitment:
            coinbase2 += encode_varint(1) + encode_varint(32) + b"\x00" * 32
        coinbase2 += struct.pack("<I", self.height - 1)  # locktime
        return coinbase1, coinbase2

    def coinbase_stripped_parts(self, en1):
        return self._coinbase_parts(en1, full=False)

    def coinbase_full_parts(self, en1):
        return self._coinbase_parts(en1, full=True)

    def coinbase_txid(self, en1, en2_bytes):
        c1, c2 = self.coinbase_stripped_parts(en1)
        return dsha256(c1 + en2_bytes + c2)

    def merkle_branches(self):
        """Sibling hashes along the coinbase path (internal order).

        The coinbase sits at index 0; its sibling subtrees never contain
        the coinbase itself, so a zero placeholder can stand in for the
        (miner-specific) coinbase txid while computing the branches.
        The miner folds: h = coinbase_txid; h = dsha256(h + branch) ...
        """
        layer = [b"\x00" * 32] + [txid for (_d, txid) in self.txs]
        branches = []
        while len(layer) > 1:
            if len(layer) % 2 == 1:
                layer.append(layer[-1])
            branches.append(layer[1])
            layer = [dsha256(layer[i] + layer[i + 1])
                     for i in range(0, len(layer), 2)]
        return branches

    def merkle_root(self, coinbase_txid):
        layer = [coinbase_txid] + [txid for (_d, txid) in self.txs]
        while len(layer) > 1:
            if len(layer) % 2 == 1:
                layer.append(layer[-1])
            layer = [dsha256(layer[i] + layer[i + 1])
                     for i in range(0, len(layer), 2)]
        return layer[0]

    def header(self, merkle_root, nonce):
        return (struct.pack("<I", self.version) + self.prevhash +
                merkle_root + struct.pack("<I", self.curtime) +
                struct.pack("<I", self.bits) + struct.pack("<I", nonce))

    def assemble_block(self, en1, en2_bytes, nonce):
        c1, c2 = self.coinbase_full_parts(en1)
        coinbase = c1 + en2_bytes + c2
        txid = self.coinbase_txid(en1, en2_bytes)
        root = self.merkle_root(txid)
        blk = self.header(root, nonce)
        blk += encode_varint(1 + len(self.txs))
        blk += coinbase
        for (data, _txid) in self.txs:
            blk += data
        return blk


# ------------------------------------------------------------------ pool state (ledger)

class State:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.data = {
            "version": 1,
            "chain": None,
            "miners": {},        # addr -> {shares, invalid, blocks, first_seen, last_seen}
            "round_shares": {},  # addr -> shares in the current (open) round
            "blocks": [],        # found blocks with round snapshot + payout status
            "payouts": [],       # payout records
            "total_shares": 0,
            "total_invalid": 0,
        }
        if os.path.exists(path):
            try:
                with open(path, "r") as f:
                    loaded = json.load(f)
                if loaded.get("version") == 1:
                    self.data = loaded
            except Exception as e:
                log("state load failed (%s); starting fresh" % e)

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f, indent=1, sort_keys=True)
        os.replace(tmp, self.path)

    def miner(self, addr):
        m = self.data["miners"].get(addr)
        if m is None:
            m = {"shares": 0, "invalid": 0, "blocks": 0,
                 "first_seen": now(), "last_seen": now()}
            self.data["miners"][addr] = m
        return m


# ------------------------------------------------------------------ pool core

class Pool:
    def __init__(self, args):
        self.args = args
        self.rpc = Rpc(args.rpc_host, args.rpc_port, datadir=args.rpc_datadir,
                       user=args.rpc_user, password=args.rpc_password,
                       wallet=args.rpc_wallet)
        self.state = State(args.state)
        self.rx = RxHelperPool(args.rxhelper, count=args.verify_helpers)
        self.chain = None
        self.pool_script = None
        self.share_target = None
        self.template = None
        self.jobs = {}            # job_id -> Template
        self.job_order = []
        self.job_counter = 0
        self.tip_hash = None
        self.tip_height = -1
        self.sessions = set()
        self.sessions_lock = threading.Lock()
        self.lock = threading.Lock()          # guards template/jobs/state touches
        self.seen_shares = set()              # (job_id, en1, en2, nonce)
        self.share_times = {}                 # addr -> [timestamps] (hashrate window)
        self.addr_cache = {}                  # addr -> (isvalid, scriptpubkey hex, ts)
        self.shutting_down = False

    # -------------------------------------------------------------- node info

    def validate_address(self, addr):
        ent = self.addr_cache.get(addr)
        if ent and now() - ent[2] < 3600:
            return ent[0], ent[1]
        try:
            r = self.rpc.call("validateaddress", [addr])
        except RpcError:
            return False, None
        ok = bool(r.get("isvalid"))
        script = r.get("scriptPubKey") if ok else None
        self.addr_cache[addr] = (ok, script, now())
        return ok, script

    def refresh_template(self, reason=""):
        gbt = self.rpc.call("getblocktemplate", [{"rules": ["segwit"]}])
        tmpl = Template(gbt, self.pool_script)
        with self.lock:
            self.job_counter += 1
            job_id = "%x" % self.job_counter
            self.jobs[job_id] = tmpl
            self.job_order.append(job_id)
            while len(self.job_order) > 4:
                old = self.job_order.pop(0)
                self.jobs.pop(old, None)
            self.template = tmpl
            self.current_job_id = job_id
            if self.args.share_target_hex:
                self.share_target = int(self.args.share_target_hex, 16)
            else:
                st = tmpl.target * self.args.share_multiplier
                self.share_target = min(st, MAX_TARGET)
        log("new job %s height=%d txs=%d target=%064x share_target=%064x (%s)"
            % (job_id, tmpl.height, len(tmpl.txs), tmpl.target,
               self.share_target, reason))
        self.broadcast_job()
        return job_id

    def job_message(self, session):
        with self.lock:
            tmpl = self.template
            job_id = self.current_job_id
        if tmpl is None:
            return None
        c1, c2 = tmpl.coinbase_stripped_parts(session.en1)
        branches = [b.hex() for b in tmpl.merkle_branches()] \
            if tmpl.txs else []
        return {"id": None, "method": "job", "params": {
            "job_id": job_id,
            "height": tmpl.height,
            "version": tmpl.version,
            "prevhash": tmpl.prevhash.hex(),       # wire (internal) order
            "coinbase1": c1.hex(),                 # stripped form, ends after extranonce1
            "coinbase2": c2.hex(),                 # stripped form, starts at sequence
            "merkle_branches": branches,           # wire order
            "time": tmpl.curtime,
            "bits": "%08x" % tmpl.bits,
            "share_target": "%064x" % self.share_target,
            "block_target": "%064x" % tmpl.target,
            "extranonce1": "%08x" % session.en1,
            "extranonce2_size": 4,
        }}

    def broadcast_job(self):
        with self.sessions_lock:
            sessions = list(self.sessions)
        for s in sessions:
            msg = self.job_message(s)
            if msg:
                s.send(msg)

    # -------------------------------------------------------------- shares

    def submit_share(self, session, params):
        addr = session.address
        job_id = params.get("job_id", "")
        en2_hex = params.get("extranonce2", "")
        nonce_hex = params.get("nonce", "")
        with self.lock:
            tmpl = self.jobs.get(job_id)
        if tmpl is None:
            return (False, "job not found")
        try:
            en2 = bytes.fromhex(en2_hex)
            nonce_bytes = bytes.fromhex(nonce_hex)
            if len(en2) != 4 or len(nonce_bytes) != 4:
                raise ValueError
            nonce = struct.unpack("<I", nonce_bytes)[0]
        except Exception:
            return (False, "malformed share")
        key = (job_id, session.en1, en2_hex.lower(), nonce_hex.lower())
        with self.lock:
            if key in self.seen_shares:
                return (False, "duplicate share")
            self.seen_shares.add(key)
            if len(self.seen_shares) > 200000:
                self.seen_shares = set(list(self.seen_shares)[-100000:])

        txid = tmpl.coinbase_txid(session.en1, en2)
        root = tmpl.merkle_root(txid)
        header = tmpl.header(root, nonce)
        h = self.rx.hash(header)
        h_int = int.from_bytes(h, "little")
        if h_int > self.share_target:
            with self.lock:
                m = self.state.miner(addr)
                m["invalid"] += 1
                m["last_seen"] = now()
                self.state.data["total_invalid"] += 1
                self.state.save()
            return (False, "low difficulty share")

        is_block = h_int <= tmpl.target
        block_hash = None
        with self.lock:
            m = self.state.miner(addr)
            m["shares"] += 1
            m["last_seen"] = now()
            self.state.data["total_shares"] += 1
            rs = self.state.data["round_shares"]
            rs[addr] = rs.get(addr, 0) + 1
            self.share_times.setdefault(addr, []).append(now())
            self.state.save()
        if is_block:
            block_hash = self._submit_block(session, tmpl, en2, nonce, h)
        return (True, {"block": bool(block_hash), "hash": h[::-1].hex()})

    def _submit_block(self, session, tmpl, en2, nonce, rx_hash):
        # Only submit if the template still builds on the current tip;
        # same-height candidates from a stale job would just fork.
        if tmpl.height != self.tip_height + 1:
            return None
        blk = tmpl.assemble_block(session.en1, en2, nonce)
        block_hash_disp = dsha256(blk[:80])[::-1].hex()
        try:
            res = self.rpc.call("submitblock", [blk.hex()])
        except RpcError as e:
            log("submitblock RPC error: %s" % e)
            return None
        if res is not None:
            log("submitblock rejected: %s (hash %s)" % (res, block_hash_disp))
            return None
        # confirm it landed as the new tip
        try:
            tip = self.rpc.call("getblockhash", [tmpl.height])
        except RpcError:
            tip = None
        if tip != block_hash_disp:
            log("submitblock accepted but tip mismatch at %d: %s != %s"
                % (tmpl.height, tip, block_hash_disp))
            return None
        log("BLOCK FOUND height=%d hash=%s by %s (rx %s)"
            % (tmpl.height, block_hash_disp, session.address,
               rx_hash[::-1].hex()))
        with self.lock:
            self.tip_height = tmpl.height
            self.tip_hash = block_hash_disp
            m = self.state.miner(session.address)
            m["blocks"] += 1
            self.state.data["blocks"].append({
                "height": tmpl.height,
                "hash": block_hash_disp,
                "finder": session.address,
                "time": now(),
                "reward_sats": tmpl.coinbasevalue,
                "round_shares": dict(self.state.data["round_shares"]),
                "matured": False,
                "paid": False,
            })
            self.state.data["round_shares"] = {}
            self.state.save()
        try:
            self.refresh_template("pool block")
        except Exception as e:
            log("template refresh after block failed: %s" % e)
        return block_hash_disp

    # -------------------------------------------------------------- payouts

    def check_payouts(self, tip_height):
        """Mature found blocks; compute (and on testnet+autopay, execute)
        proportional payouts from each block's round shares."""
        due = []
        with self.lock:
            for b in self.state.data["blocks"]:
                if not b["matured"] and tip_height >= b["height"] + MATURITY:
                    b["matured"] = True
                    due.append(b)
            if due:
                self.state.save()
        for b in due:
            self._pay_block(b)

    def _pay_block(self, b):
        total = sum(b["round_shares"].values())
        reward = b["reward_sats"]
        fee = reward * self.args.pool_fee_percent // 100
        distributable = reward - fee
        amounts = {}
        skipped = {}
        if total > 0:
            for addr, cnt in sorted(b["round_shares"].items()):
                amt = distributable * cnt // total
                if amt < DUST:
                    skipped[addr] = amt
                else:
                    amounts[addr] = amt
        paid_total = sum(amounts.values())
        record = {
            "height": b["height"],
            "block_hash": b["hash"],
            "time": now(),
            "reward_sats": reward,
            "fee_sats": fee,
            "amounts_sats": amounts,
            "skipped_dust_sats": skipped,
            "paid_total_sats": paid_total,
            "txid": None,
            "mode": "manual",
        }
        execute = (self.chain == "test" and self.args.autopay and amounts)
        if execute:
            send = {}
            for addr, sats in amounts.items():
                # JSON number with exactly 8 decimals (built as raw text
                # below via a placeholder-free manual params string is
                # overkill; float with round(x,8) is exact enough for
                # satoshi values < 2^53 and Core parses decimals exactly).
                send[addr] = round(sats / COIN, 8)
            try:
                txid = self.rpc.call("sendmany", ["", send],
                                     wallet=self.args.rpc_wallet)
                record["txid"] = txid
                record["mode"] = "autopay"
                log("payout for block %d executed: %s sats=%d txid=%s"
                    % (b["height"], amounts, paid_total, txid))
            except RpcError as e:
                record["mode"] = "autopay-failed"
                record["error"] = str(e)
                log("payout for block %d FAILED: %s" % (b["height"], e))
        else:
            log("payout computed for block %d (mode=%s): amounts=%s"
                % (b["height"], record["mode"], amounts))
        with self.lock:
            b["paid"] = record["mode"] in ("autopay", "manual")
            b["payout"] = record
            self.state.data["payouts"].append(record)
            self.state.save()

    # -------------------------------------------------------------- monitor

    def monitor_loop(self):
        last_template_at = 0
        while not self.shutting_down:
            try:
                info = self.rpc.call("getblockchaininfo")
                self.chain = info["chain"]
                tip = info["bestblockhash"]
                height = info["blocks"]
                self.tip_height = height
                if tip != self.tip_hash:
                    self.tip_hash = tip
                    self.refresh_template("tip change")
                    last_template_at = time.time()
                    self.check_payouts(height)
                elif time.time() - last_template_at > 60:
                    self.refresh_template("periodic")
                    last_template_at = time.time()
            except Exception as e:
                log("monitor: %s" % e)
            time.sleep(2)

    # -------------------------------------------------------------- stats

    def stats(self):
        with self.sessions_lock:
            sessions = list(self.sessions)
        with self.lock:
            data = self.state.data
            share_target = self.share_target or 0
            miners = {}
            total_hr = 0.0
            window = 600
            cutoff = now() - window
            for addr, m in data["miners"].items():
                times = [t for t in self.share_times.get(addr, [])
                         if t >= cutoff]
                self.share_times[addr] = times
                hr = 0.0
                if times and share_target:
                    hashes_per_share = (1 << 256) / (share_target + 1)
                    span = max(now() - times[0], 1)
                    hr = len(times) * hashes_per_share / span
                total_hr += hr
                miners[addr] = {
                    "shares": m["shares"], "invalid": m["invalid"],
                    "blocks": m["blocks"],
                    "hashrate_est": round(hr, 2),
                    "connected": any(s.address == addr for s in sessions),
                    "last_seen": iso(m["last_seen"]),
                }
            return {
                "chain": self.chain,
                "tip_height": self.tip_height,
                "connected_miners": len(sessions),
                "hashrate_est_total": round(total_hr, 2),
                "total_shares": data["total_shares"],
                "total_invalid": data["total_invalid"],
                "blocks_found": len(data["blocks"]),
                "blocks": data["blocks"],
                "payouts": data["payouts"],
                "round_shares": data["round_shares"],
                "miners": miners,
                "updated_at": iso(now()),
            }


# ------------------------------------------------------------------ stratum-like server

class Session(socketserver.BaseRequestHandler):
    en1_counter = 0
    en1_lock = threading.Lock()

    def setup(self):
        with Session.en1_lock:
            Session.en1_counter += 1
            self.en1 = Session.en1_counter
        self.address = None
        self.worker = None
        self.buf = b""
        self.send_lock = threading.Lock()
        self.pool = self.server.pool

    def send(self, obj):
        line = (json.dumps(obj) + "\n").encode()
        with self.send_lock:
            try:
                self.request.sendall(line)
            except OSError:
                pass

    def reply(self, rid, result=None, error=None):
        self.send({"id": rid, "result": result, "error": error})

    def handle(self):
        with self.pool.sessions_lock:
            self.pool.sessions.add(self)
        log("connect from %s en1=%d" % (self.client_address, self.en1))
        try:
            while True:
                data = self.request.recv(65536)
                if not data:
                    break
                self.buf += data
                while b"\n" in self.buf:
                    line, self.buf = self.buf.split(b"\n", 1)
                    line = line.strip()
                    if line:
                        self.handle_line(line)
        except OSError:
            pass
        finally:
            with self.pool.sessions_lock:
                self.pool.sessions.discard(self)
            log("disconnect %s (%s)" % (self.client_address, self.address))

    def handle_line(self, line):
        try:
            msg = json.loads(line.decode())
        except Exception:
            return
        method = msg.get("method")
        rid = msg.get("id")
        params = msg.get("params") or {}
        if method == "login":
            login = params.get("login", "")
            addr, _, worker = login.partition(".")
            ok, _script = self.pool.validate_address(addr)
            if not ok:
                self.reply(rid, error="invalid address")
                return
            self.address = addr
            self.worker = worker or None
            with self.pool.lock:
                self.pool.state.miner(addr)
                self.pool.state.save()
            self.reply(rid, result={"status": "ok",
                                    "extranonce1": "%08x" % self.en1})
            job = self.pool.job_message(self)
            if job:
                self.send(job)
        elif method == "submit":
            if not self.address:
                self.reply(rid, error="not logged in")
                return
            ok, info = self.pool.submit_share(self, params)
            if ok:
                self.reply(rid, result={"status": "accepted", **info})
            else:
                self.reply(rid, error=info)
        elif method == "getjob":
            if not self.address:
                self.reply(rid, error="not logged in")
                return
            job = self.pool.job_message(self)
            if job:
                self.send(job)
            self.reply(rid, result={"status": "ok"})
        else:
            self.reply(rid, error="unknown method")


class StratumServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, pool):
        self.pool = pool
        super().__init__(addr, Session)


class StatsHandler(BaseHTTPRequestHandler):
    pool = None

    def do_GET(self):
        if self.path.split("?")[0] in ("/stats.json", "/stats"):
            body = json.dumps(self.pool.stats(), indent=1).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *a):
        pass


# ------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser(description="1919coin mining pool v1")
    ap.add_argument("--pool-address", required=True,
                    help="address receiving coinbases (pool wallet)")
    ap.add_argument("--port", type=int, default=13333, help="miner TCP port")
    ap.add_argument("--http-port", type=int, default=18080)
    ap.add_argument("--rpc-host", default="127.0.0.1")
    ap.add_argument("--rpc-port", type=int, default=None,
                    help="default: 11450 main / 21450 test (by --chain-hint)")
    ap.add_argument("--chain-hint", choices=["main", "test"], default="test",
                    help="only used to pick the default RPC port; the "
                         "authoritative chain is read from the node")
    ap.add_argument("--rpc-datadir", default=None,
                    help="node datadir (for .cookie); testnet cookie lives "
                         "in <datadir>/testnet3 - pass that directory")
    ap.add_argument("--rpc-user", default=None)
    ap.add_argument("--rpc-password", default=None)
    ap.add_argument("--rpc-wallet", default=None,
                    help="wallet name for sendmany (testnet payouts)")
    ap.add_argument("--state", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "pool-state.json"))
    ap.add_argument("--rxhelper", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "rxhelper"))
    ap.add_argument("--verify-helpers", type=int, default=2)
    ap.add_argument("--share-multiplier", type=int, default=4,
                    help="share_target = block_target * N (capped); "
                         "ignored when --share-target-hex is given")
    ap.add_argument("--share-target-hex", default=None)
    ap.add_argument("--pool-fee-percent", type=int, default=0)
    ap.add_argument("--autopay", action="store_true",
                    help="execute sendmany payouts (TESTNET ONLY; hard "
                         "guard refuses on any other chain)")
    args = ap.parse_args()
    if args.rpc_port is None:
        args.rpc_port = 21450 if args.chain_hint == "test" else 11450

    pool = Pool(args)

    info = pool.rpc.call("getblockchaininfo")
    pool.chain = info["chain"]
    if pool.chain == "main" and args.autopay:
        log("WARNING: --autopay given on mainnet; payouts will be computed "
            "and recorded only (execution is testnet-only by design)")
    ok, script = pool.validate_address(args.pool_address)
    if not ok:
        log("pool address %s is not valid on chain %s"
            % (args.pool_address, pool.chain))
        return 1
    pool.pool_script = bytes.fromhex(script)
    pool.state.data["chain"] = pool.chain
    pool.state.save()
    pool.tip_hash = info["bestblockhash"]
    pool.tip_height = info["blocks"]
    log("connected to node: chain=%s height=%d pool_address=%s"
        % (pool.chain, pool.tip_height, args.pool_address))
    pool.refresh_template("startup")

    mon = threading.Thread(target=pool.monitor_loop, daemon=True)
    mon.start()

    StatsHandler.pool = pool
    httpd = ThreadingHTTPServer(("0.0.0.0", args.http_port), StatsHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    log("stats on http://0.0.0.0:%d/stats.json" % args.http_port)

    server = StratumServer(("0.0.0.0", args.port), pool)
    log("pool listening on 0.0.0.0:%d" % args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    pool.shutting_down = True
    return 0


if __name__ == "__main__":
    sys.exit(main())
