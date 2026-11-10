"""Admin dashboard: JSON API + static single-page app."""
import asyncio
import ipaddress
import os
import re
import secrets
import time

from aiohttp import web

from . import config as C
from . import firewall, network
from .config import hash_password, verify_password
from .util import normalize_domain, normalize_list, registrable

STATIC = os.path.join(os.path.dirname(__file__), "static")
COOKIE = "pishield_session"
SESSION_TTL = 14 * 24 * 3600

# Sites whose ads come from the same servers as the content: DNS can't strip those.
FIRST_PARTY_ADS = {
    "youtube.com": "YouTube", "youtu.be": "YouTube", "facebook.com": "Facebook",
    "instagram.com": "Instagram", "twitch.tv": "Twitch", "x.com": "X", "twitter.com": "X",
    "tiktok.com": "TikTok", "reddit.com": "Reddit", "hulu.com": "Hulu", "spotify.com": "Spotify",
    "linkedin.com": "LinkedIn", "pinterest.com": "Pinterest", "snapchat.com": "Snapchat",
}
# Background/CDN domains that are never a "website you visited".
INFRA = {
    "apple.com", "icloud.com", "apple-dns.net", "aaplimg.com", "mzstatic.com", "cdn-apple.com",
    "icloud-content.com", "apple-cloudkit.com", "push.apple.com", "googleapis.com", "gstatic.com",
    "googleusercontent.com", "gvt1.com", "gvt2.com", "gvt3.com", "ggpht.com", "googlevideo.com",
    "cloudflare.com", "cloudflare-dns.com", "akamaihd.net", "akamaized.net", "akamaiedge.net",
    "akadns.net", "edgekey.net", "edgesuite.net", "cloudfront.net", "amazonaws.com", "fastly.net",
    "fastly-edge.com", "fbcdn.net", "cdninstagram.com", "msftconnecttest.com", "msftncsi.com",
    "windowsupdate.com", "live.com", "microsoft.com", "office.net", "msn.com", "azureedge.net",
    "ntp.org", "pishield.lan", "arpa", "whatsapp.net", "whatsapp.com", "samsung.com",
    "samsungcloud.com", "xiaomi.com", "googlesyndication.com", "jsdelivr.net", "unpkg.com",
    "cloudflareinsights.com", "sentry.io", "gravatar.com", "wp.com", "doubleclick.net",
    "google-analytics.com", "googletagmanager.com", "pishield", "lan", "local", "connectivitycheck.gstatic.com",
}


def _ip_ok_for_dashboard(cfg, ip):
    if C.DEV or cfg["dashboard_from_upstream"]:
        return True
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped:
        a = a.ipv4_mapped
    return a.is_loopback or a in ipaddress.ip_network(C.HOTSPOT_NET)


def err(msg, status=400):
    return web.json_response({"error": msg}, status=status)


def create_app(ctx):
    cfg, lists, resolver, stats = ctx["cfg"], ctx["lists"], ctx["resolver"], ctx["stats"]
    sessions = {}  # token -> expiry
    fails = {}  # ip -> (count, last)

    @web.middleware
    async def guard(request, handler):
        if not _ip_ok_for_dashboard(cfg, request.remote or ""):
            return web.Response(status=403, text="The PiShield dashboard is only reachable from the PiShield Wi-Fi.")
        if request.path.startswith("/api/"):
            if request.method not in ("GET", "HEAD") and request.headers.get("X-PiShield") != "1":
                return err("bad request", 400)  # blocks cross-site form posts
            if request.path not in ("/api/login", "/api/session"):
                tok = request.cookies.get(COOKIE)
                if not tok or sessions.get(tok, 0) < time.time():
                    return err("Not signed in", 401)
        try:
            return await handler(request)
        except (ValueError, RuntimeError) as e:
            return err(str(e))

    def save_and_reload():
        cfg.save()
        resolver.refresh_rules()

    async def body(request):
        try:
            data = await request.json()
        except Exception:
            data = {}
        return data if isinstance(data, dict) else {}

    # ---------- auth ----------
    async def session(request):
        tok = request.cookies.get(COOKIE)
        return web.json_response({"signed_in": bool(tok and sessions.get(tok, 0) > time.time())})

    async def login(request):
        ip = request.remote or ""
        n, last = fails.get(ip, (0, 0))
        if n >= 5 and time.time() - last < 60:
            return err("Too many attempts. Wait a minute.", 429)
        data = await body(request)
        if not verify_password(str(data.get("password", "")), cfg["admin_hash"]):
            fails[ip] = (n + 1, time.time())
            await asyncio.sleep(1)
            return err("Wrong password", 401)
        fails.pop(ip, None)
        tok = secrets.token_urlsafe(32)
        sessions[tok] = time.time() + SESSION_TTL
        resp = web.json_response({"ok": True})
        resp.set_cookie(COOKIE, tok, max_age=SESSION_TTL, httponly=True, samesite="Strict")
        return resp

    async def logout(request):
        sessions.pop(request.cookies.get(COOKIE, ""), None)
        resp = web.json_response({"ok": True})
        resp.del_cookie(COOKIE)
        return resp

    async def change_password(request):
        data = await body(request)
        if not verify_password(str(data.get("current", "")), cfg["admin_hash"]):
            return err("Current password is wrong")
        new = str(data.get("new", ""))
        if len(new) < 6:
            return err("New password must be at least 6 characters")
        cfg["admin_hash"] = hash_password(new)
        cfg.save()
        keep = request.cookies.get(COOKIE)
        for t in list(sessions):
            if t != keep:
                sessions.pop(t)
        return web.json_response({"ok": True})

    # ---------- overview ----------
    def device_names():
        names = {ip: (l["hostname"] or l["mac"]) for ip, l in network.leases().items()}
        names.setdefault("127.0.0.1", "This Pi")
        return names

    async def overview(request):
        series = stats.series()
        q24 = sum(s[1] for s in series)
        b24 = sum(s[2] for s in series)
        now = time.time()
        online = sum(1 for c, v in stats.clients.items() if now - v["last"] < 600 and c != "127.0.0.1")
        names = device_names()
        return web.json_response({
            "mode": cfg["mode"],
            "queries_24h": q24, "blocked_24h": b24,
            "devices_online": online,
            "series": series,
            "top_blocked": stats.top_blocked.most_common(8),
            "active": [{"device": names.get(c, c), "sites": sorted(resolver.active_sites(c))}
                       for c in list(resolver.activity) if resolver.active_sites(c)],
            "blocklist_domains": len(lists.domains),
            "lists_updating": lists.updating,
            "sites_enabled": sum(1 for s in cfg["sites"] if s.get("enabled", True)),
            "upstreams": resolver.upstreams(),
            "uptime": int(now - stats.started),
            "health": network.health(),
        })

    async def set_mode(request):
        mode = (await body(request)).get("mode")
        if mode not in ("off", "sites", "everywhere"):
            return err("Unknown mode")
        cfg["mode"] = mode
        save_and_reload()
        return web.json_response({"mode": mode})

    # ---------- sites ----------
    def site_view(s):
        names = device_names()
        on = [names.get(c, c) for c in list(resolver.activity) if s["domain"] in resolver.active_sites(c)]
        return {**s, "blocked": stats.site_blocks.get(s["domain"], 0), "active_on": on,
                "warning": FIRST_PARTY_ADS.get(s["domain"])}

    async def sites_get(request):
        have = {s["domain"] for s in cfg["sites"]}
        sugg = []
        for d, n in stats.visited.most_common(200):
            if d in have or d in INFRA or "." not in d or resolver.is_ad(d):
                continue
            sugg.append({"domain": d, "count": n})
            if len(sugg) >= 18:
                break
        return web.json_response({
            "mode": cfg["mode"], "window": cfg["site_window"],
            "sites": [site_view(s) for s in cfg["sites"]],
            "suggestions": sugg,
        })

    def find_site(domain):
        return next((s for s in cfg["sites"] if s["domain"] == domain), None)

    async def sites_add(request):
        data = await body(request)
        d = normalize_domain(str(data.get("domain", "")), strip_www=True)
        if not d:
            return err("That doesn't look like a website address")
        if find_site(d):
            return err(f"{d} is already in the list")
        site = {"domain": d, "enabled": True, "extra": [], "added": int(time.time())}
        cfg["sites"].append(site)
        save_and_reload()
        return web.json_response(site_view(site))

    async def sites_patch(request):
        s = find_site(request.match_info["domain"])
        if not s:
            return err("No such site", 404)
        data = await body(request)
        if "enabled" in data:
            s["enabled"] = bool(data["enabled"])
        if "extra" in data:
            raw = data["extra"] if isinstance(data["extra"], list) else re.split(r"[\s,]+", str(data["extra"]))
            s["extra"] = normalize_list(raw)
        save_and_reload()
        return web.json_response(site_view(s))

    async def sites_delete(request):
        d = request.match_info["domain"]
        cfg["sites"] = [s for s in cfg["sites"] if s["domain"] != d]
        save_and_reload()
        return web.json_response({"ok": True})

    # ---------- activity ----------
    async def activity(request):
        status = request.query.get("status", "")
        q = request.query.get("q", "").strip().lower()
        limit = min(int(request.query.get("limit", "200")), 1000)
        names = device_names()
        rows = []
        for ts, client, name, qtype, st, reason, ms in reversed(stats.log):
            if status and st != status:
                continue
            dev = names.get(client, client)
            if q and q not in name and q not in dev.lower():
                continue
            rows.append({"ts": ts, "client": client, "device": dev, "name": name, "type": qtype,
                         "status": st, "reason": reason, "ms": ms})
            if len(rows) >= limit:
                break
        return web.json_response({"rows": rows, "total": stats.total, "blocked": stats.blocked})

    async def quick_rule(request):
        data = await body(request)
        d = normalize_domain(str(data.get("domain", "")))
        kind = request.match_info["kind"]
        if not d:
            return err("Invalid domain")
        add, remove = ("allowlist", "blocklist") if kind == "allow" else ("blocklist", "allowlist")
        cfg[remove] = [x for x in cfg[remove] if x != d]
        if d not in cfg[add]:
            cfg[add].append(d)
        save_and_reload()
        return web.json_response({"ok": True, "domain": d})

    # ---------- filters ----------
    async def filters_get(request):
        out = []
        for l in cfg["lists"]:
            st = lists.status.get(l["id"], {})
            out.append({**l, "count": st.get("count", 0), "updated": st.get("updated", 0), "error": st.get("error")})
        return web.json_response({"lists": out, "allowlist": cfg["allowlist"], "blocklist": cfg["blocklist"],
                                  "total": len(lists.domains), "updating": lists.updating,
                                  "lists_updated": cfg["lists_updated"]})

    async def lists_add(request):
        data = await body(request)
        url = str(data.get("url", "")).strip()
        if not re.match(r"^https?://\S+$", url):
            return err("Enter an http(s) URL")
        name = str(data.get("name", "")).strip()[:80] or url.split("/")[2]
        lid = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "list"
        base, i = lid, 2
        while any(l["id"] == lid for l in cfg["lists"]):
            lid, i = f"{base}-{i}", i + 1
        cfg["lists"].append({"id": lid, "name": name, "url": url, "enabled": True})
        cfg.save()
        asyncio.ensure_future(lists.update(only=lid))
        return web.json_response({"ok": True, "id": lid})

    async def lists_patch(request):
        lid = request.match_info["id"]
        l = next((x for x in cfg["lists"] if x["id"] == lid), None)
        if not l:
            return err("No such list", 404)
        data = await body(request)
        if "enabled" in data:
            l["enabled"] = bool(data["enabled"])
        cfg.save()
        if l["enabled"] and not os.path.exists(lists.path(lid)):
            asyncio.ensure_future(lists.update(only=lid))
        else:
            await asyncio.to_thread(lists.rebuild)
        return web.json_response({"ok": True})

    async def lists_delete(request):
        lid = request.match_info["id"]
        cfg["lists"] = [x for x in cfg["lists"] if x["id"] != lid]
        cfg.save()
        try:
            os.remove(lists.path(lid))
        except OSError:
            pass
        await asyncio.to_thread(lists.rebuild)
        return web.json_response({"ok": True})

    async def lists_update(request):
        asyncio.ensure_future(lists.update())
        return web.json_response({"ok": True})

    async def set_rules(request):
        kind = request.match_info["kind"]  # allowlist | blocklist
        data = await body(request)
        raw = data.get("domains", [])
        if isinstance(raw, str):
            raw = re.split(r"[\s,]+", raw)
        cfg[kind] = normalize_list(raw)
        save_and_reload()
        return web.json_response({kind: cfg[kind]})

    # ---------- network ----------
    async def net_get(request):
        st, sv, hs = await asyncio.gather(network.status(cfg), network.saved(), network.hotspot_get())
        return web.json_response({"status": st, "saved": sv, "hotspot": hs})

    async def net_scan(request):
        return web.json_response({"networks": await network.scan(cfg)})

    async def net_connect(request):
        data = await body(request)
        ssid = str(data.get("ssid", ""))
        if not ssid:
            return err("Pick a network")
        if data.get("enterprise"):
            msg = await network.connect_enterprise(
                cfg, ssid, str(data.get("identity", ""))[:200], str(data.get("password", ""))[:200],
                str(data.get("eap", "peap")), str(data.get("phase2", "mschapv2")),
                str(data.get("domain", ""))[:253], str(data.get("anonymous", ""))[:200], bool(data.get("hidden")))
        else:
            msg = await network.connect(cfg, ssid, str(data.get("password", "")), bool(data.get("hidden")))
        resolver._ups_ts = 0  # pick up the new network's DNS servers
        return web.json_response({"ok": True, "message": msg})

    async def net_activate(request):
        await network.activate(cfg, str((await body(request)).get("uuid", "")))
        resolver._ups_ts = 0
        return web.json_response({"ok": True})

    async def net_forget(request):
        await network.forget(str((await body(request)).get("uuid", "")))
        return web.json_response({"ok": True})

    async def hotspot_put(request):
        data = await body(request)
        await network.hotspot_set(str(data.get("ssid", "")).strip(), str(data.get("password", "")),
                                  str(data.get("band", "bg")), data.get("channel", 6))
        return web.json_response({"ok": True})

    # ---------- devices ----------
    async def devices(request):
        leases = network.leases()
        now = time.time()
        ips = set(leases) | {c for c in stats.clients if c != "127.0.0.1"}
        out = []
        for ip in ips:
            l = leases.get(ip, {})
            c = stats.clients.get(ip, {"q": 0, "b": 0, "last": 0})
            out.append({"ip": ip, "mac": l.get("mac", ""), "hostname": l.get("hostname", ""),
                        "queries": c["q"], "blocked": c["b"], "last_seen": c["last"],
                        "online": now - c["last"] < 600 if c["last"] else False,
                        "on_sites": sorted(resolver.active_sites(ip))})
        out.sort(key=lambda d: (-d["online"], -d["last_seen"]))
        return web.json_response({"devices": out})

    # ---------- settings ----------
    async def settings_get(request):
        return web.json_response({k: cfg[k] for k in ("site_window", "upstream_dns", "force_dns",
                                                       "dashboard_from_upstream", "mode")})

    async def settings_put(request):
        data = await body(request)
        fw = False
        if "site_window" in data:
            w = int(data["site_window"])
            if not 15 <= w <= 3600:
                return err("Site window must be between 15 and 3600 seconds")
            cfg["site_window"] = w
        if "upstream_dns" in data:
            v = str(data["upstream_dns"]).strip() or "auto"
            if v != "auto":
                for x in re.split(r"[\s,]+", v):
                    ipaddress.IPv4Address(x)  # raises ValueError -> 400
            cfg["upstream_dns"] = v
            resolver._ups_ts = 0
        for k in ("force_dns", "dashboard_from_upstream"):
            if k in data:
                cfg[k] = bool(data[k])
                fw = True
        save_and_reload()
        if fw:
            await firewall.apply(cfg)
        return await settings_get(request)

    async def system(request):
        action = request.match_info["action"]
        if action not in ("reboot", "shutdown", "restart"):
            return err("Unknown action")
        await network.system_action(action)
        return web.json_response({"ok": True})

    # ---------- static ----------
    async def index(request):
        resp = web.FileResponse(os.path.join(STATIC, "index.html"))
        resp.headers["Cache-Control"] = "no-store"
        return resp

    app = web.Application(middlewares=[guard])
    r = app.router
    r.add_get("/", index)
    r.add_static("/static", STATIC)
    r.add_get("/api/session", session)
    r.add_post("/api/login", login)
    r.add_post("/api/logout", logout)
    r.add_post("/api/password", change_password)
    r.add_get("/api/overview", overview)
    r.add_post("/api/mode", set_mode)
    r.add_get("/api/sites", sites_get)
    r.add_post("/api/sites", sites_add)
    r.add_patch("/api/sites/{domain}", sites_patch)
    r.add_delete("/api/sites/{domain}", sites_delete)
    r.add_get("/api/activity", activity)
    r.add_post("/api/rule/{kind:allow|block}", quick_rule)
    r.add_get("/api/filters", filters_get)
    r.add_post("/api/lists", lists_add)
    r.add_post("/api/lists/update", lists_update)
    r.add_patch("/api/lists/{id}", lists_patch)
    r.add_delete("/api/lists/{id}", lists_delete)
    r.add_put("/api/rules/{kind:allowlist|blocklist}", set_rules)
    r.add_get("/api/network", net_get)
    r.add_post("/api/network/scan", net_scan)
    r.add_post("/api/network/connect", net_connect)
    r.add_post("/api/network/activate", net_activate)
    r.add_post("/api/network/forget", net_forget)
    r.add_put("/api/hotspot", hotspot_put)
    r.add_get("/api/devices", devices)
    r.add_get("/api/settings", settings_get)
    r.add_put("/api/settings", settings_put)
    r.add_post("/api/system/{action}", system)
    return app
