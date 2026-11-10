"""The DNS server: answers hotspot clients, blocks ad domains, forwards the rest upstream.

How "selected sites" mode works
-------------------------------
DNS never sees which page a request belongs to, so PiShield infers it per device:
when a device looks up a selected site (cnn.com, www.cnn.com, static.cnn.com ...)
that device is marked "on cnn.com" for `site_window` seconds, refreshed on every
lookup of that site. While a device is on a selected site, ad/tracker domains from
the blocklists (plus that site's extra domains) are blocked for that device only.

To keep that accurate, answers for selected sites and for ad domains that were let
through are given very short TTLs, so devices keep asking and the decision is
re-made as you move between sites.
"""
import asyncio
import ipaddress
import logging
import random
import re
import struct
import time
from collections import Counter, defaultdict, deque

from dnslib import AAAA, QTYPE, RCODE, RR, A, DNSRecord

from . import config as C
from .util import registrable, suffix_match

log = logging.getLogger("pishield.dns")

LOCAL_NAMES = {"pishield", "pishield.lan", "pi.shield"}
# Answering NXDOMAIN for these tells Firefox to turn off its built-in DNS-over-HTTPS
# and Apple devices to turn off iCloud Private Relay for this network, so lookups
# actually come through PiShield.
CANARY = {"use-application-dns.net", "mask.icloud.com", "mask-h2.icloud.com"}
REVERSE_LOCAL = ".50.168.192.in-addr.arpa"
FALLBACK_UPSTREAMS = ["1.1.1.1", "9.9.9.9"]

SITE_TTL = 10      # TTL for answers about a selected site
WATCH_TTL = 5      # TTL for ad domains that were allowed (sites mode)
BLOCK_TTL_SITES = 10
BLOCK_TTL_ALL = 60
CACHE_MAX = 20000

HOTSPOT = ipaddress.ip_network(C.HOTSPOT_NET)


def client_allowed(ip: str) -> bool:
    if C.DEV:
        return True
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped:
        a = a.ipv4_mapped
    return a.is_loopback or a in HOTSPOT


class Stats:
    def __init__(self):
        self.started = time.time()
        self.total = 0
        self.blocked = 0
        self.top_blocked = Counter()
        self.visited = Counter()  # registrable domains of allowed lookups
        self.site_blocks = Counter()  # selected site -> ads blocked while on it
        self.clients = {}  # ip -> {"q", "b", "last"}
        self.log = deque(maxlen=3000)
        self.buckets = {}  # 10-minute bucket start -> [queries, blocked]

    def record(self, client, name, qtype, status, reason, ms):
        now = time.time()
        self.total += 1
        blocked = status == "blocked"
        c = self.clients.setdefault(client, {"q": 0, "b": 0, "last": 0})
        c["q"] += 1
        c["last"] = now
        if blocked:
            self.blocked += 1
            c["b"] += 1
            self.top_blocked[name] += 1
            if reason.startswith("on "):
                self.site_blocks[reason[3:]] += 1
        elif status == "allowed":
            self.visited[registrable(name)] += 1
        b = int(now // 600) * 600
        bucket = self.buckets.setdefault(b, [0, 0])
        bucket[0] += 1
        if blocked:
            bucket[1] += 1
        self.log.append((round(now, 2), client, name, qtype, status, reason, round(ms, 1)))
        if len(self.buckets) > 160:
            for k in sorted(self.buckets)[:-150]:
                del self.buckets[k]
        for ctr in (self.top_blocked, self.visited):
            if len(ctr) > 20000:
                keep = ctr.most_common(5000)
                ctr.clear()
                ctr.update(dict(keep))

    def series(self):
        now = int(time.time() // 600) * 600
        return [[t, *self.buckets.get(t, [0, 0])] for t in range(now - 143 * 600, now + 1, 600)]


class _UDPQuery(asyncio.DatagramProtocol):
    def __init__(self, fut):
        self.fut = fut

    def datagram_received(self, data, addr):
        if not self.fut.done():
            self.fut.set_result(data)

    def error_received(self, exc):
        if not self.fut.done():
            self.fut.set_exception(exc)


async def udp_query(data, server, timeout):
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    transport, _ = await loop.create_datagram_endpoint(lambda: _UDPQuery(fut), remote_addr=(server, 53))
    try:
        transport.sendto(data)
        return await asyncio.wait_for(fut, timeout)
    finally:
        transport.close()


async def tcp_query(data, server, timeout):
    reader, writer = await asyncio.wait_for(asyncio.open_connection(server, 53), timeout)
    try:
        writer.write(struct.pack("!H", len(data)) + data)
        await writer.drain()
        (ln,) = struct.unpack("!H", await asyncio.wait_for(reader.readexactly(2), timeout))
        return await asyncio.wait_for(reader.readexactly(ln), timeout)
    finally:
        writer.close()


class Resolver:
    def __init__(self, cfg, lists, stats):
        self.cfg = cfg
        self.lists = lists
        self.stats = stats
        self.activity = defaultdict(dict)  # client ip -> {site: last lookup ts}
        self.cache = {}  # (name, qtype) -> (expires, stored_at, bytes)
        self._ups = []
        self._ups_ts = 0.0
        self.refresh_rules()

    # ---------- rules ----------
    def refresh_rules(self):
        self.allow = frozenset(self.cfg["allowlist"])
        self.block = frozenset(self.cfg["blocklist"])
        sites, extras = {}, defaultdict(set)
        for s in self.cfg["sites"]:
            if not s.get("enabled", True):
                continue
            sites[s["domain"]] = s
            for e in s.get("extra", []):
                extras[e].add(s["domain"])
        self.sites = sites
        self.extras = dict(extras)
        self.cache.clear()

    def upstreams(self):
        now = time.time()
        if now - self._ups_ts < 15 and self._ups:
            return self._ups
        conf = (self.cfg["upstream_dns"] or "auto").strip()
        cands = []
        if conf != "auto":
            cands = [x for x in re.split(r"[\s,]+", conf) if x]
        else:
            try:
                with open("/etc/resolv.conf") as f:
                    for line in f:
                        parts = line.split()
                        if len(parts) >= 2 and parts[0] == "nameserver":
                            cands.append(parts[1])
            except OSError:
                pass
        ups = []
        for x in cands:
            try:
                a = ipaddress.ip_address(x.split("%")[0])
            except ValueError:
                continue
            if a.is_loopback or str(a) == C.HOTSPOT_ADDR or a.version != 4:
                continue
            ups.append(str(a))
        self._ups = ups[:3] or list(FALLBACK_UPSTREAMS)
        self._ups_ts = now
        return self._ups

    def active_sites(self, client, now=None):
        now = now or time.time()
        win = float(self.cfg["site_window"])
        return {s: t for s, t in self.activity.get(client, {}).items() if now - t < win and s in self.sites}

    def is_ad(self, name):
        return bool(suffix_match(name, self.lists.domains)) and not suffix_match(name, self.lists.allow)

    def decide(self, client, name, now):
        """-> (action, reason, ttl_cap). action: local | nx | block | forward"""
        if name in LOCAL_NAMES:
            return "local", "dashboard", None
        if name.endswith(REVERSE_LOCAL):
            return "nx", "local", None
        if self.cfg["force_dns"] and name in CANARY:
            return "nx", "encrypted-DNS off", None
        mode = self.cfg["mode"]
        if mode == "off":
            return "forward", "", None
        if suffix_match(name, self.allow):
            return "forward", "allowlist", None
        if suffix_match(name, self.block):
            return "block", "always block", None
        ad = self.is_ad(name)
        site = None if ad else suffix_match(name, self.sites)
        if site:
            self.activity[client][site] = now
            return "forward", f"site {site}", SITE_TTL if mode == "sites" else None
        extra = suffix_match(name, self.extras)
        if mode == "everywhere":
            if ad:
                return "block", "blocklist", None
            if extra:
                return "block", f"extra for {sorted(self.extras[extra])[0]}", None
            return "forward", "", None
        # selected-sites mode
        if ad or extra:
            active = self.active_sites(client, now)
            if extra:
                owners = self.extras[extra] & active.keys()
                if owners:
                    return "block", "on " + max(owners, key=active.get), None
            if ad and active:
                return "block", "on " + max(active, key=active.get), None
            return "forward", "ad (not on a selected site)", WATCH_TTL
        return "forward", "", None

    def prune(self):
        now = time.time()
        win = float(self.cfg["site_window"])
        for client in list(self.activity):
            sites = {s: t for s, t in self.activity[client].items() if now - t < win}
            if sites:
                self.activity[client] = sites
            else:
                del self.activity[client]
        for k in [k for k, v in self.cache.items() if v[0] <= now]:
            del self.cache[k]

    # ---------- query handling ----------
    async def handle(self, data, client, udp=True):
        t0 = time.perf_counter()
        try:
            req = DNSRecord.parse(data)
        except Exception:
            return None
        if not req.questions:
            return None
        q = req.q
        name = str(q.qname).rstrip(".").lower()
        qtype = q.qtype
        now = time.time()
        action, reason, cap = self.decide(client, name, now)

        if action == "local":
            reply = req.reply()
            if qtype in (QTYPE.A, QTYPE.ANY):
                reply.add_answer(RR(q.qname, QTYPE.A, rdata=A(C.HOTSPOT_ADDR), ttl=60))
            out, status = reply.pack(), "local"
        elif action == "nx":
            reply = req.reply()
            reply.header.rcode = RCODE.NXDOMAIN
            out, status = reply.pack(), "local"
        elif action == "block":
            ttl = BLOCK_TTL_SITES if self.cfg["mode"] == "sites" else BLOCK_TTL_ALL
            reply = req.reply()
            if qtype == QTYPE.A:
                reply.add_answer(RR(q.qname, QTYPE.A, rdata=A("0.0.0.0"), ttl=ttl))
            elif qtype == QTYPE.AAAA:
                reply.add_answer(RR(q.qname, QTYPE.AAAA, rdata=AAAA("::"), ttl=ttl))
            out, status = reply.pack(), "blocked"
        else:
            out = await self.forward(req, data, name, qtype, cap)
            status = "allowed"
            if out is None:
                reply = req.reply()
                reply.header.rcode = RCODE.SERVFAIL
                out, status, reason = reply.pack(), "error", "no upstream answer"

        if udp:
            max_len = 512
            for rr in req.ar:
                if rr.rtype == QTYPE.OPT:
                    max_len = max(512, min(int(rr.rclass), 4096))
            if len(out) > max_len:
                reply = req.reply()
                reply.header.tc = 1
                out = reply.pack()

        self.stats.record(client, name, QTYPE.get(qtype, str(qtype)), status, reason,
                          (time.perf_counter() - t0) * 1000)
        return out

    async def forward(self, req, data, name, qtype, cap):
        key = (name, qtype, req.q.qclass)
        now = time.time()
        hit = self.cache.get(key)
        if hit and hit[0] > now:
            return self._finalize(hit[2], req.header.id, int(now - hit[1]), cap)

        qid = random.randint(0, 0xFFFF)
        out_q = struct.pack("!H", qid) + data[2:]
        resp = None
        for server in self.upstreams():
            try:
                r = await udp_query(out_q, server, 2.5)
                if len(r) < 12 or r[:2] != out_q[:2]:
                    continue
                if r[2] & 0x02:  # truncated -> retry over TCP
                    r = await tcp_query(out_q, server, 4.0)
                resp = r
                break
            except Exception as e:
                log.debug("upstream %s failed for %s: %s", server, name, e)
        if resp is None:
            return None
        try:
            parsed = DNSRecord.parse(resp)
        except Exception:
            return None

        rcode = parsed.header.rcode
        if rcode in (RCODE.NOERROR, RCODE.NXDOMAIN):
            ttls = [rr.ttl for rr in parsed.rr + parsed.auth if rr.rtype != QTYPE.OPT]
            ttl = min(ttls) if ttls else 60
            ttl = max(0, min(ttl, 3600 if parsed.rr else 300))
            if ttl > 0:
                if len(self.cache) >= CACHE_MAX:
                    self.prune()
                    if len(self.cache) >= CACHE_MAX:
                        for k in list(self.cache)[: CACHE_MAX // 4]:
                            del self.cache[k]
                self.cache[key] = (now + ttl, now, resp)
        return self._finalize(resp, req.header.id, 0, cap, parsed)

    @staticmethod
    def _finalize(resp, client_id, age, cap, parsed=None):
        if age == 0 and cap is None:
            return struct.pack("!H", client_id) + resp[2:]
        rec = parsed or DNSRecord.parse(resp)
        rec.header.id = client_id
        for rr in rec.rr + rec.auth + rec.ar:
            if rr.rtype == QTYPE.OPT:
                continue
            t = max(0, rr.ttl - age)
            if cap is not None:
                t = min(t, cap)
            rr.ttl = t
        return rec.pack()


class UDPServer(asyncio.DatagramProtocol):
    def __init__(self, resolver):
        self.resolver = resolver
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        if client_allowed(addr[0]):
            asyncio.ensure_future(self._respond(data, addr))

    async def _respond(self, data, addr):
        try:
            out = await self.resolver.handle(data, addr[0], udp=True)
            if out:
                self.transport.sendto(out, addr)
        except Exception:
            log.exception("query failed")


def tcp_handler(resolver):
    async def handle(reader, writer):
        peer = (writer.get_extra_info("peername") or ("",))[0]
        try:
            if not client_allowed(peer):
                return
            while True:
                (ln,) = struct.unpack("!H", await asyncio.wait_for(reader.readexactly(2), 15))
                data = await asyncio.wait_for(reader.readexactly(ln), 15)
                out = await resolver.handle(data, peer, udp=False)
                if not out:
                    return
                writer.write(struct.pack("!H", len(out)) + out)
                await writer.drain()
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError):
            pass
        except Exception:
            log.exception("tcp query failed")
        finally:
            writer.close()

    return handle
