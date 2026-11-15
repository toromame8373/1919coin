#!/usr/bin/env python3
"""seedd.py - lightweight seed-only daemon for 1919coin (main + test).

Seed-only model: this daemon does NOT store, validate or relay the chain.
It only
  * answers P2P handshakes (version/verack) on 11451 (main) / 21451 (test),
  * exchanges peer addresses (getaddr/addr) so real nodes can find each other,
  * answers ping with pong,
  * remembers which peers it has seen (handshake-confirmed or gossiped),
  * remembers which peer first announced each new block (inv) - used as a
    rough "who is mining" estimate,
  * periodically crawls known peers to check liveness / start_height,
  * serves network statistics as JSON over HTTP (port 8080, CORS *).

Bitcoin P2P wire format, 1919coin magics, protocol 70016.
Python standard library only.

BIP324 note: stock Core >= 26 initiates outbound connections (including
-addnode) with the v2 transport. This daemon is v1-only; as responder it
therefore checks the first 4 bytes before sending anything and silently
closes non-v1 initiations, which makes Core retry the connection with
v1 (V2Transport::ShouldReconnectV1). As initiator (crawl) it speaks v1,
which Core responders detect and accept (KEY_MAYBE_V1).
"""

import argparse
import hashlib
import ipaddress
import json
import os
import random
import signal
import socket
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROTOCOL_VERSION = 70016
USER_AGENT = "/1919seed:1.0.0/"
MAX_PAYLOAD = 4 * 1024 * 1024
MSG_BLOCK = 2
WITNESS_FLAG = 0x40000000
DAY = 86400

NETWORKS = {
    "main": {"magic": bytes.fromhex("1919810a"), "port": 11451},
    "test": {"magic": bytes.fromhex("1919810b"), "port": 21451},
}


def dsha256(b):
    return hashlib.sha256(hashlib.sha256(b).digest()).digest()


def now():
    return int(time.time())


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def log(msg):
    print("[%s] %s" % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), msg),
          flush=True)


# ---------------------------------------------------------------- wire helpers

def encode_varint(n):
    if n < 0xfd:
        return struct.pack("<B", n)
    if n <= 0xffff:
        return b"\xfd" + struct.pack("<H", n)
    if n <= 0xffffffff:
        return b"\xfe" + struct.pack("<I", n)
    return b"\xff" + struct.pack("<Q", n)


def decode_varint(buf, off):
    if off >= len(buf):
        raise ValueError("varint eof")
    b = buf[off]
    if b < 0xfd:
        return b, off + 1
    if b == 0xfd:
        return struct.unpack_from("<H", buf, off + 1)[0], off + 3
    if b == 0xfe:
        return struct.unpack_from("<I", buf, off + 1)[0], off + 5
    return struct.unpack_from("<Q", buf, off + 1)[0], off + 9


def ip_to_bytes(ip):
    try:
        return b"\x00" * 10 + b"\xff\xff" + socket.inet_aton(ip)
    except OSError:
        return socket.inet_pton(socket.AF_INET6, ip)


def ip_from_bytes(b):
    if len(b) != 16:
        return "0.0.0.0"
    if b[:12] == b"\x00" * 10 + b"\xff\xff":
        return socket.inet_ntoa(b[12:])
    return str(ipaddress.IPv6Address(b))


def pack_netaddr(services, ip, port):
    return struct.pack("<Q", services) + ip_to_bytes(ip) + struct.pack(">H", port)


def parse_netaddr(buf, off):
    services = struct.unpack_from("<Q", buf, off)[0]
    ip = ip_from_bytes(buf[off + 8:off + 24])
    port = struct.unpack_from(">H", buf, off + 24)[0]
    return services, ip, port


def build_message(magic, command, payload):
    return (magic + command.encode("ascii").ljust(12, b"\x00")
            + struct.pack("<I", len(payload)) + dsha256(payload)[:4] + payload)


def build_version_payload(listen_port, remote_ip, remote_port, start_height):
    p = struct.pack("<i", PROTOCOL_VERSION)
    p += struct.pack("<Q", 0)                    # services: none (seed only)
    p += struct.pack("<q", now())
    p += pack_netaddr(0, remote_ip, remote_port)  # addr_recv: the peer
    p += pack_netaddr(0, "0.0.0.0", listen_port)  # addr_from: us
    p += struct.pack("<Q", random.getrandbits(64))
    ua = USER_AGENT.encode()
    p += encode_varint(len(ua)) + ua
    p += struct.pack("<i", start_height)
    p += b"\x00"                                  # relay: off
    return p


def parse_version_payload(payload):
    if len(payload) < 80:
        raise ValueError("short version payload")
    ver, services = struct.unpack_from("<iQ", payload, 0)
    _, fip, fport = parse_netaddr(payload, 46)    # addr_from of the sender
    off = 80
    ualen, off = decode_varint(payload, off)
    ua = payload[off:off + ualen].decode("utf-8", "replace")
    off += ualen
    height = 0
    if len(payload) >= off + 4:
        height = struct.unpack_from("<i", payload, off)[0]
    return {"protocol": ver, "services": services, "from_ip": fip,
            "from_port": fport, "ua": ua, "height": height}


# ---------------------------------------------------------------- state

class NetState:
    def __init__(self):
        self.peers = {}   # "ip:port" -> {ip,port,ua,height,services,first,lc,lg}
        self.blocks = {}  # hash hex -> {"by": ip, "at": ts}
        self.best_height = 0
        self.live = set()  # ids of currently open connections


class Seed:
    def __init__(self, state_path, public_ip):
        self.lock = threading.Lock()
        self.nets = {name: NetState() for name in NETWORKS}
        self.state_path = state_path
        self.public_ip = public_ip
        self.stop = False
        self._conn_seq = 0
        self.load()

    # ---- persistence
    def load(self):
        try:
            with open(self.state_path, "r") as f:
                data = json.load(f)
            for name, ns in self.nets.items():
                d = data.get(name, {})
                ns.peers = d.get("peers", {})
                ns.blocks = d.get("blocks", {})
                ns.best_height = int(d.get("best_height", 0))
            log("state loaded from %s" % self.state_path)
        except FileNotFoundError:
            log("no state file yet, starting fresh")
        except Exception as e:
            log("state load failed (%s), starting fresh" % e)

    def save(self):
        t = now()
        with self.lock:
            data = {}
            for name, ns in self.nets.items():
                # prune: blocks older than 7d, peers unseen for 30d
                ns.blocks = {h: b for h, b in ns.blocks.items()
                             if b["at"] >= t - 7 * DAY}
                ns.peers = {k: p for k, p in ns.peers.items()
                            if max(p["lc"], p["lg"]) >= t - 30 * DAY}
                data[name] = {"peers": ns.peers, "blocks": ns.blocks,
                              "best_height": ns.best_height}
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.rename(tmp, self.state_path)

    # ---- peer bookkeeping
    def note_peer(self, net, ip, port, ua=None, height=None, services=None,
                  confirmed=False, gossip_ts=None):
        if not ip or ip in ("0.0.0.0", "::"):
            return None
        key = "%s:%d" % (ip, port)
        t = now()
        with self.lock:
            ns = self.nets[net]
            p = ns.peers.get(key)
            if p is None:
                if len(ns.peers) >= 5000:
                    oldest = min(ns.peers,
                                 key=lambda k: max(ns.peers[k]["lc"],
                                                   ns.peers[k]["lg"]))
                    del ns.peers[oldest]
                p = {"ip": ip, "port": int(port), "ua": "", "height": 0,
                     "services": 0, "first": t, "lc": 0, "lg": 0}
                ns.peers[key] = p
            if ua:
                p["ua"] = ua[:80]
            if height is not None:
                p["height"] = int(height)
                if height > ns.best_height:
                    ns.best_height = int(height)
            if services is not None:
                p["services"] = int(services)
            if confirmed:
                p["lc"] = t
            if gossip_ts is not None:
                g = int(gossip_ts)
                if g <= 0 or g > t:
                    g = t
                if g > p["lg"]:
                    p["lg"] = g
        return key

    def note_block(self, net, hashhex, by_ip):
        with self.lock:
            ns = self.nets[net]
            if hashhex not in ns.blocks:
                ns.blocks[hashhex] = {"by": by_ip, "at": now()}
                log("%s: new block %s… first announced by %s"
                    % (net, hashhex[:16], by_ip))

    def addr_entries(self, net, exclude_key, limit=100):
        """Peers to hand out in an addr reply: confirmed first, then gossip."""
        with self.lock:
            ns = self.nets[net]
            cand = [p for k, p in ns.peers.items()
                    if k != exclude_key and p["port"] > 0]
        cand.sort(key=lambda p: (p["lc"], p["lg"]), reverse=True)
        out = b""
        n = 0
        for p in cand[:limit]:
            ts = p["lc"] or p["lg"]
            out += struct.pack("<I", ts)
            out += pack_netaddr(p["services"], p["ip"], p["port"])
            n += 1
        return encode_varint(n) + out

    def stats(self):
        t = now()
        out = {"updated_at": iso(t)}
        with self.lock:
            for name, ns in self.nets.items():
                known = sum(1 for p in ns.peers.values()
                            if p["lc"] >= t - DAY)
                miners = len({b["by"] for b in ns.blocks.values()
                              if b["at"] >= t - DAY})
                out[name] = {
                    "known_peers_24h": known,
                    "connected_now": len(ns.live),
                    "best_height": ns.best_height,
                    "total_mined_yju": ns.best_height * 10,
                    "miners_24h_est": miners,
                }
        return out


# ---------------------------------------------------------------- connection

class Conn:
    def __init__(self, seed, sock, remote_ip, remote_port, net, outbound,
                 deadline=None):
        self.seed = seed
        self.sock = sock
        self.remote_ip = remote_ip
        self.remote_port = remote_port
        self.net = net
        self.outbound = outbound
        self.deadline = deadline
        self.buf = b""
        self.peer_key = None
        self.got_version = False
        with seed.lock:
            seed._conn_seq += 1
            self.cid = seed._conn_seq

    # -- io
    def read_exact(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("eof")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send_msg(self, command, payload):
        magic = NETWORKS[self.net]["magic"]
        self.sock.sendall(build_message(magic, command, payload))

    # -- main loop
    def run(self):
        seed = self.seed
        ns = seed.nets[self.net]
        with seed.lock:
            ns.live.add(self.cid)
        try:
            self.sock.settimeout(240)
            listen_port = NETWORKS[self.net]["port"]
            with seed.lock:
                start_h = ns.best_height
            if not self.outbound:
                # Responder side: inspect the first 4 bytes BEFORE sending
                # anything. A v1 initiator starts with the network magic.
                # A BIP324 v2 initiator (stock Bitcoin Core >= 26 outbound,
                # including every -addnode connection) starts with an
                # ElligatorSwift key instead. Core only falls back to v1
                # (V2Transport::ShouldReconnectV1) when it has received
                # NOTHING from us and the connection dies in its initial
                # state - so for non-v1 initiations we must close silently,
                # without sending our version first.
                first = self.read_exact(4)
                if first != NETWORKS[self.net]["magic"]:
                    log("%s: non-v1 initiation from %s (probable BIP324 v2), "
                        "closing without reply so the client retries with v1"
                        % (self.net, self.remote_ip))
                    return
                self.buf = first + self.buf
            self.send_msg("version", build_version_payload(
                listen_port, self.remote_ip, self.remote_port, start_h))
            while not seed.stop:
                if self.deadline and time.time() > self.deadline:
                    break
                header = self.read_exact(24)
                magic = NETWORKS[self.net]["magic"]
                if header[:4] != magic:
                    log("%s: bad magic from %s, closing" % (self.net,
                                                            self.remote_ip))
                    break
                command = header[4:16].split(b"\x00")[0].decode("ascii",
                                                                "replace")
                length = struct.unpack_from("<I", header, 16)[0]
                if length > MAX_PAYLOAD:
                    log("%s: oversize payload %d from %s, closing"
                        % (self.net, length, self.remote_ip))
                    break
                checksum = header[20:24]
                payload = self.read_exact(length) if length else b""
                if dsha256(payload)[:4] != checksum:
                    log("%s: bad checksum from %s, closing"
                        % (self.net, self.remote_ip))
                    break
                if not self.dispatch(command, payload):
                    break
        except (ConnectionError, socket.timeout, OSError):
            pass
        except Exception as e:
            log("%s: conn error with %s: %r" % (self.net, self.remote_ip, e))
        finally:
            with seed.lock:
                ns.live.discard(self.cid)
            try:
                self.sock.close()
            except OSError:
                pass

    def dispatch(self, command, payload):
        seed = self.seed
        if command == "version":
            try:
                v = parse_version_payload(payload)
            except Exception:
                return False
            self.got_version = True
            if v["from_port"] > 0 and v["from_ip"] not in ("0.0.0.0", "::"):
                adv_ip, adv_port = v["from_ip"], v["from_port"]
            else:
                adv_ip, adv_port = self.remote_ip, 0
            self.peer_key = seed.note_peer(
                self.net, adv_ip, adv_port, ua=v["ua"], height=v["height"],
                services=v["services"], confirmed=True)
            # also mark the source IP as seen (same host, listen port may
            # differ from the advertised one in odd NAT setups)
            log("%s: version from %s (adv %s:%d) ua=%r height=%d proto=%d"
                % (self.net, self.remote_ip, adv_ip, adv_port, v["ua"],
                   v["height"], v["protocol"]))
            self.send_msg("verack", b"")
        elif command == "verack":
            if self.outbound:
                try:
                    self.send_msg("getaddr", b"")
                except OSError:
                    return False
        elif command == "ping":
            if len(payload) == 8:
                self.send_msg("pong", payload)
        elif command == "getaddr":
            payload = seed.addr_entries(self.net, self.peer_key)
            cnt, _ = decode_varint(payload, 0)
            log("%s: getaddr from %s -> addr with %d entries"
                % (self.net, self.remote_ip, cnt))
            self.send_msg("addr", payload)
        elif command == "addr":
            self.handle_addr(payload)
        elif command == "inv":
            self.handle_inv(payload)
        # everything else (sendheaders, sendcmpct, feefilter, getheaders,
        # mempool, ...) is intentionally ignored: we are a seed, not a node.
        return True

    def handle_addr(self, payload):
        try:
            count, off = decode_varint(payload, 0)
        except Exception:
            return
        count = min(count, 1000)
        net_port = NETWORKS[self.net]["port"]
        for _ in range(count):
            if off + 30 > len(payload):
                break
            ts = struct.unpack_from("<I", payload, off)[0]
            services, ip, port = parse_netaddr(payload, off + 4)
            off += 30
            if port <= 0:
                continue
            if ip == self.seed.public_ip and port == net_port:
                continue  # do not learn ourselves
            self.seed.note_peer(self.net, ip, port, services=services,
                                gossip_ts=ts)

    def handle_inv(self, payload):
        try:
            count, off = decode_varint(payload, 0)
        except Exception:
            return
        count = min(count, 50000)
        for _ in range(count):
            if off + 36 > len(payload):
                break
            typ = struct.unpack_from("<I", payload, off)[0]
            h = payload[off + 4:off + 36]
            off += 36
            if typ & ~WITNESS_FLAG == MSG_BLOCK:
                self.seed.note_block(self.net, h.hex(), self.remote_ip)


# ---------------------------------------------------------------- servers

def accept_loop(seed, net):
    port = NETWORKS[net]["port"]
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", port))
    srv.listen(128)
    srv.settimeout(1.0)
    log("%s: listening on 0.0.0.0:%d" % (net, port))
    while not seed.stop:
        try:
            conn, addr = srv.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        ns = seed.nets[net]
        with seed.lock:
            too_many = len(ns.live) >= 256
        if too_many:
            conn.close()
            continue
        c = Conn(seed, conn, addr[0], addr[1], net, outbound=False)
        threading.Thread(target=c.run, daemon=True).start()
    srv.close()


def crawl_loop(seed, interval):
    time.sleep(45)  # let inbound peers register first
    while not seed.stop:
        for net in NETWORKS:
            with seed.lock:
                ns = seed.nets[net]
                live_keys = set()
                cands = [(p["ip"], p["port"]) for p in ns.peers.values()
                         if p["port"] > 0]
            random.shuffle(cands)
            for ip, port in cands[:16]:
                if seed.stop:
                    break
                if (ip, port) in live_keys:
                    continue
                try:
                    s = socket.create_connection((ip, port), timeout=6)
                except OSError:
                    continue
                c = Conn(seed, s, ip, port, net, outbound=True,
                         deadline=time.time() + 20)
                threading.Thread(target=c.run, daemon=True).start()
                time.sleep(0.3)
        for _ in range(int(interval)):
            if seed.stop:
                break
            time.sleep(1)


def save_loop(seed, interval):
    while not seed.stop:
        for _ in range(int(interval)):
            if seed.stop:
                break
            time.sleep(1)
        if not seed.stop:
            try:
                seed.save()
            except Exception as e:
                log("state save failed: %r" % e)


class StatsHandler(BaseHTTPRequestHandler):
    seed = None

    def do_GET(self):
        if self.path.split("?")[0] == "/stats.json":
            body = json.dumps(self.seed.stats()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            body = b'{"error":"not found"}'
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, *args):
        pass


def main():
    ap = argparse.ArgumentParser(description="1919coin seed daemon")
    ap.add_argument("--state", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "state.json"))
    ap.add_argument("--main-port", type=int, default=11451)
    ap.add_argument("--test-port", type=int, default=21451)
    ap.add_argument("--http-port", type=int, default=8080)
    ap.add_argument("--public-ip", default="141.147.146.142")
    ap.add_argument("--crawl-interval", type=int, default=300)
    ap.add_argument("--save-interval", type=int, default=60)
    args = ap.parse_args()

    NETWORKS["main"]["port"] = args.main_port
    NETWORKS["test"]["port"] = args.test_port

    seed = Seed(args.state, args.public_ip)

    def handle_signal(signum, frame):
        seed.stop = True
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    for net in NETWORKS:
        threading.Thread(target=accept_loop, args=(seed, net),
                         daemon=True).start()
    threading.Thread(target=crawl_loop, args=(seed, args.crawl_interval),
                     daemon=True).start()
    threading.Thread(target=save_loop, args=(seed, args.save_interval),
                     daemon=True).start()

    StatsHandler.seed = seed
    httpd = ThreadingHTTPServer(("0.0.0.0", args.http_port), StatsHandler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    log("stats http listening on 0.0.0.0:%d" % args.http_port)

    while not seed.stop:
        time.sleep(0.5)
    try:
        seed.save()
        log("state saved, exiting")
    except Exception as e:
        log("final save failed: %r" % e)
    httpd.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
