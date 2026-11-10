"""Download, cache and parse ad/tracker domain lists."""
import asyncio
import logging
import os
import re
import time

import aiohttp

from . import config as C
from .util import DOMAIN_RE

log = logging.getLogger("pishield.lists")

SKIP = {
    "localhost", "localhost.localdomain", "local", "broadcasthost", "ip6-localhost",
    "ip6-loopback", "ip6-localnet", "ip6-mcastprefix", "ip6-allnodes", "ip6-allrouters",
    "ip6-allhosts", "0.0.0.0",
}
HOST_IPS = {"0.0.0.0", "127.0.0.1", "::", "::1", "0", "255.255.255.255", "fe80::1%lo0", "ff00::0", "ff02::1", "ff02::2", "ff02::3"}
MAX_LIST_BYTES = 60 * 1024 * 1024


def _clean(d):
    d = d.lower().rstrip(".")
    if d.startswith("*."):
        d = d[2:]
    if d in SKIP or not DOMAIN_RE.match(d):
        return None
    return d


def parse(text: str):
    """Understands hosts files, plain domain lists and simple adblock '||domain^' rules."""
    block, allow = set(), set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in "#![":
            continue
        if line.startswith("@@||"):
            body = line[4:]
            if body.endswith("^") or body.endswith("^|"):
                d = _clean(body.split("^", 1)[0])
                if d:
                    allow.add(d)
            continue
        if line.startswith("||"):
            body = line[2:]
            if not body.endswith("^"):  # has a path or $modifiers -> not a plain domain rule
                continue
            d = _clean(body[:-1])
            if d:
                block.add(d)
            continue
        line = line.split("#", 1)[0].strip()
        parts = line.split()
        if not parts:
            continue
        if parts[0] in HOST_IPS:
            names = parts[1:]
        elif len(parts) == 1:
            names = parts
        else:
            continue
        for n in names:
            d = _clean(n)
            if d:
                block.add(d)
    return block, allow


def safe_id(s):
    return re.sub(r"[^a-z0-9_-]", "_", s.lower())[:60] or "list"


class Blocklists:
    def __init__(self, cfg):
        self.cfg = cfg
        self.domains = frozenset()
        self.allow = frozenset()
        self.status = {}  # id -> {"count", "updated", "error"}
        self.updating = False
        self.last_attempt = 0.0
        self._parsed = {}  # id -> (mtime, block, allow)

    def path(self, lid):
        return os.path.join(C.LISTS_DIR, safe_id(lid) + ".txt")

    def rebuild(self):
        """Re-read cached list files and swap in the merged domain set."""
        dom, alw = set(), set()
        for lst in self.cfg["lists"]:
            lid = lst["id"]
            st = self.status.setdefault(lid, {})
            p = self.path(lid)
            if not os.path.exists(p):
                st.update(count=0, updated=0)
                continue
            mtime = os.path.getmtime(p)
            cached = self._parsed.get(lid)
            if not cached or cached[0] != mtime:
                with open(p, encoding="utf-8", errors="ignore") as f:
                    b, a = parse(f.read())
                cached = (mtime, b, a)
                self._parsed[lid] = cached
            st.update(count=len(cached[1]), updated=mtime)
            if lst.get("enabled"):
                dom |= cached[1]
                alw |= cached[2]
        live = {l["id"] for l in self.cfg["lists"]}
        for lid in list(self._parsed):
            if lid not in live:
                self._parsed.pop(lid, None)
                self.status.pop(lid, None)
        self.domains = frozenset(dom)
        self.allow = frozenset(alw)
        log.info("blocklists loaded: %d domains (%d exceptions)", len(self.domains), len(self.allow))

    async def update(self, only=None):
        """Download enabled lists (or just `only`), then rebuild."""
        if self.updating:
            return
        self.updating = True
        self.last_attempt = time.time()
        ok = 0
        try:
            os.makedirs(C.LISTS_DIR, exist_ok=True)
            timeout = aiohttp.ClientTimeout(total=120)
            async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": "PiShield/1.0"}) as s:
                for lst in list(self.cfg["lists"]):
                    if only and lst["id"] != only:
                        continue
                    if not only and not lst.get("enabled"):
                        continue
                    st = self.status.setdefault(lst["id"], {})
                    try:
                        async with s.get(lst["url"]) as r:
                            r.raise_for_status()
                            chunks, size = [], 0
                            async for chunk in r.content.iter_chunked(256 * 1024):
                                size += len(chunk)
                                if size > MAX_LIST_BYTES:
                                    raise ValueError("list is larger than 60 MB")
                                chunks.append(chunk)
                            body = b"".join(chunks)
                        text = body.decode("utf-8", "ignore")
                        b, _ = await asyncio.to_thread(parse, text)
                        if not b:
                            raise ValueError("no domains found in that file")
                        p = self.path(lst["id"])
                        with open(p + ".tmp", "w", encoding="utf-8") as f:
                            f.write(text)
                        os.replace(p + ".tmp", p)
                        st["error"] = None
                        ok += 1
                        log.info("downloaded %s (%d domains)", lst["id"], len(b))
                    except Exception as e:  # keep the old copy on failure
                        st["error"] = str(e) or e.__class__.__name__
                        log.warning("list %s failed: %s", lst["id"], st["error"])
            if not only and ok:
                self.cfg["lists_updated"] = time.time()
                self.cfg.save()
            await asyncio.to_thread(self.rebuild)
        finally:
            self.updating = False

    async def auto_update_loop(self):
        """Refresh every 24h; retry every 15 min while nothing is loaded (e.g. no internet yet)."""
        while True:
            age = time.time() - float(self.cfg.get("lists_updated") or 0)
            stale = age > 24 * 3600
            empty = not self.domains and any(l.get("enabled") for l in self.cfg["lists"])
            if (stale or empty) and time.time() - self.last_attempt > 15 * 60:
                try:
                    await self.update()
                except Exception:
                    log.exception("list update crashed")
            await asyncio.sleep(60)
