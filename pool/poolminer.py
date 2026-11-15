#!/usr/bin/env python3
"""poolminer.py - dedicated miner client for the 1919coin pool (poold.py).

Speaks the pool's JSON-Lines protocol (see README.md): login with a
payout address, receive jobs (coinbase parts + merkle branches), roll
extranonce2/nonce, compute RandomX over the 80-byte header via rxhelper
subprocesses, submit shares.

This is a correctness-first test miner, not a performance miner:
hashing runs in light mode by default (one rxhelper per --helpers
worker). --fast uses a single rxhelper with the 2 GiB dataset.

Options of note:
  --max-shares N    exit after N accepted shares (default: unlimited)
  --duration SEC    exit after SEC seconds
  --bad-share-test  right after login, send three shares that MUST be
                    rejected (below-target nonce, duplicate, unknown
                    job) and print the pool's answers; then mine on.
"""

import argparse
import hashlib
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time


def dsha256(b):
    return hashlib.sha256(hashlib.sha256(b).digest()).digest()


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


class RxWorker(threading.Thread):
    """One rxhelper subprocess = one hashing thread."""

    def __init__(self, binary, fast, work_fn, index, nworkers):
        super().__init__(daemon=True)
        self.binary = binary
        self.fast = fast
        self.work_fn = work_fn
        self.index = index
        self.nworkers = nworkers
        self.proc = None

    def hash(self, header):
        line = header.hex().encode() + b"\n"
        self.proc.stdin.write(line)
        self.proc.stdin.flush()
        out = self.proc.stdout.readline().strip()
        return bytes.fromhex(out.decode())

    def run(self):
        args = [self.binary] + (["--fast"] if self.fast else [])
        self.proc = subprocess.Popen(args, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL)
        self.work_fn(self, self.index, self.nworkers)


class Job:
    def __init__(self, params):
        self.job_id = params["job_id"]
        self.height = params["height"]
        self.version = params["version"]
        self.prevhash = bytes.fromhex(params["prevhash"])
        self.coinbase1 = bytes.fromhex(params["coinbase1"])
        self.coinbase2 = bytes.fromhex(params["coinbase2"])
        self.branches = [bytes.fromhex(b) for b in params["merkle_branches"]]
        self.time = params["time"]
        self.bits = int(params["bits"], 16)
        self.share_target = int(params["share_target"], 16)
        self.block_target = int(params["block_target"], 16)

    def header(self, en2, nonce):
        coinbase = self.coinbase1 + en2 + self.coinbase2
        h = dsha256(coinbase)
        for br in self.branches:
            h = dsha256(h + br)
        return (struct.pack("<I", self.version) + self.prevhash + h +
                struct.pack("<I", self.time) + struct.pack("<I", self.bits) +
                struct.pack("<I", nonce))


class Miner:
    def __init__(self, args):
        self.args = args
        self.sock = None
        self.buf = b""
        self.job = None
        self.job_lock = threading.Lock()
        self.send_lock = threading.Lock()
        self.next_id = 0
        self.id_lock = threading.Lock()
        self.pending = {}       # id -> (Event, result holder)
        self.accepted = 0
        self.rejected = 0
        self.hashes = 0
        self.started = time.time()
        self.stop = False
        self.en2 = args.en2_start

    # ------------------------------------------------------------ net

    def send(self, obj):
        with self.send_lock:
            self.sock.sendall((json.dumps(obj) + "\n").encode())

    def request(self, method, params, timeout=30):
        with self.id_lock:
            self.next_id += 1
            rid = self.next_id
        ev = threading.Event()
        holder = {}
        self.pending[rid] = (ev, holder)
        self.send({"id": rid, "method": method, "params": params})
        if not ev.wait(timeout):
            return {"error": "timeout"}
        return holder

    def reader_loop(self):
        while not self.stop:
            try:
                data = self.sock.recv(65536)
            except OSError:
                break
            if not data:
                break
            self.buf += data
            while b"\n" in self.buf:
                line, self.buf = self.buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line.decode())
                except Exception:
                    continue
                if msg.get("method") == "job":
                    with self.job_lock:
                        self.job = Job(msg["params"])
                    log("job %s height=%d share_target=%064x"
                        % (self.job.job_id, self.job.height,
                           self.job.share_target))
                elif msg.get("id") in self.pending:
                    ev, holder = self.pending.pop(msg["id"])
                    holder.update(msg)
                    ev.set()
        self.stop = True

    # ------------------------------------------------------------ mining

    def work(self, rx, index, nworkers):
        nonce = 0
        en2_val = self.en2 + index
        en2 = struct.pack("<I", en2_val)
        cur_job = None
        while not self.stop:
            with self.job_lock:
                job = self.job
            if job is None:
                time.sleep(0.2)
                continue
            if job is not cur_job:
                cur_job = job
                nonce = 0
                en2_val = self.en2 + index
                en2 = struct.pack("<I", en2_val)
            header = job.header(en2, nonce)
            h = rx.hash(header)
            self.hashes += 1
            h_int = int.from_bytes(h, "little")
            if h_int <= job.share_target:
                self.submit(job, en2, nonce, want_block=h_int <= job.block_target)
            nonce += 1
            if nonce > 0xffffffff:
                nonce = 0
                en2_val += nworkers
                en2 = struct.pack("<I", en2_val)
            if self.args.max_shares and self.accepted >= self.args.max_shares:
                self.stop = True
            if self.args.duration and \
                    time.time() - self.started > self.args.duration:
                self.stop = True

    def submit(self, job, en2, nonce, want_block=False):
        resp = self.request("submit", {
            "job_id": job.job_id,
            "extranonce2": en2.hex(),
            "nonce": struct.pack("<I", nonce).hex(),
        })
        if resp.get("error"):
            self.rejected += 1
            log("share REJECTED: %s" % resp["error"])
        else:
            self.accepted += 1
            blk = resp.get("result", {}).get("block")
            log("share accepted (%d)%s" % (self.accepted,
                                           " *** BLOCK ***" if blk else ""))

    # ------------------------------------------------------------ bad-share probes

    def bad_share_test(self):
        with self.job_lock:
            job = self.job
        if job is None:
            log("bad-share-test: no job yet, skipped")
            return
        log("bad-share-test: probing rejection paths")
        # 1) a nonce whose hash is above the share target (found locally)
        en2 = struct.pack("<I", 0xdead)
        nonce = 0
        while True:
            h = int.from_bytes(
                self._probe_hash(job.header(en2, nonce)), "little")
            if h > job.share_target:
                break
            nonce += 1
        r = self.request("submit", {"job_id": job.job_id,
                                    "extranonce2": en2.hex(),
                                    "nonce": struct.pack("<I", nonce).hex()})
        log("bad-share-test [low difficulty] -> %s" % r.get("error", r))
        # 2) duplicate: submit one valid share twice. Find a valid nonce.
        nonce = 0
        while True:
            h = int.from_bytes(
                self._probe_hash(job.header(en2, nonce)), "little")
            if h <= job.share_target:
                break
            nonce += 1
        p = {"job_id": job.job_id, "extranonce2": en2.hex(),
             "nonce": struct.pack("<I", nonce).hex()}
        r1 = self.request("submit", p)
        r2 = self.request("submit", p)
        log("bad-share-test [first]  -> %s" % r1.get("result", r1.get("error")))
        log("bad-share-test [duplicate] -> %s" % r2.get("error", r2))
        if not r1.get("error"):
            self.accepted += 1
        # 3) unknown job id
        r = self.request("submit", {"job_id": "nope",
                                    "extranonce2": en2.hex(),
                                    "nonce": "00000000"})
        log("bad-share-test [unknown job] -> %s" % r.get("error", r))

    _probe = None

    def _probe_hash(self, header):
        if self._probe is None:
            self._probe = subprocess.Popen(
                [self.args.rxhelper], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self._probe.stdin.write(header.hex().encode() + b"\n")
        self._probe.stdin.flush()
        return bytes.fromhex(self._probe.stdout.readline().strip().decode())

    # ------------------------------------------------------------ main

    def run(self):
        self.sock = socket.create_connection(
            (self.args.host, self.args.port), timeout=30)
        self.sock.settimeout(None)
        threading.Thread(target=self.reader_loop, daemon=True).start()
        resp = self.request("login", {"login": self.args.address,
                                      "agent": "poolminer/1.0"})
        if resp.get("error"):
            log("login failed: %s" % resp["error"])
            return 1
        log("logged in as %s" % self.args.address)
        # wait for the first job
        for _ in range(50):
            with self.job_lock:
                if self.job:
                    break
            time.sleep(0.2)
        if self.args.bad_share_test:
            self.bad_share_test()
        workers = []
        n = 1 if self.args.fast else self.args.helpers
        for i in range(n):
            w = RxWorker(self.args.rxhelper, self.args.fast, self.work, i, n)
            w.start()
            workers.append(w)
        last = time.time()
        last_hashes = 0
        while not self.stop:
            time.sleep(2)
            if time.time() - last >= 10:
                rate = (self.hashes - last_hashes) / (time.time() - last)
                log("hashrate ~%.1f H/s, hashes=%d accepted=%d rejected=%d"
                    % (rate, self.hashes, self.accepted, self.rejected))
                last = time.time()
                last_hashes = self.hashes
        log("done: hashes=%d accepted=%d rejected=%d"
            % (self.hashes, self.accepted, self.rejected))
        return 0


def main():
    ap = argparse.ArgumentParser(description="1919coin pool miner (test client)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=13333)
    ap.add_argument("--address", required=True, help="payout address (login)")
    ap.add_argument("--rxhelper", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "rxhelper"))
    ap.add_argument("--helpers", type=int, default=2,
                    help="light-mode hashing workers")
    ap.add_argument("--fast", action="store_true",
                    help="single fast-mode worker (2 GiB dataset)")
    ap.add_argument("--max-shares", type=int, default=0)
    ap.add_argument("--duration", type=int, default=0, help="seconds")
    ap.add_argument("--en2-start", type=int, default=0)
    ap.add_argument("--bad-share-test", action="store_true")
    args = ap.parse_args()
    return Miner(args).run()


if __name__ == "__main__":
    sys.exit(main())
