"""Wi-Fi control through NetworkManager (nmcli): upstream network, hotspot, devices, health."""
import asyncio
import glob
import os
import re
import time

from . import config as C


async def run(*args, timeout=30):
    try:
        p = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except FileNotFoundError:
        return 127, "", f"{args[0]} is not installed"
    try:
        out, err = await asyncio.wait_for(p.communicate(), timeout)
    except asyncio.TimeoutError:
        p.kill()
        return 124, "", "timed out"
    return p.returncode, out.decode(errors="replace"), err.decode(errors="replace")


def split_terse(line):
    """Split one line of `nmcli -t` output (':' separated, '\\:' escaped)."""
    out, cur, esc = [], [], False
    for ch in line:
        if esc:
            cur.append(ch)
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return out


def _read(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


async def perm_mac(iface):
    rc, out, _ = await run("ethtool", "-P", iface, timeout=5)
    m = re.search(r"([0-9a-f]{2}(?::[0-9a-f]{2}){5})", out.lower()) if rc == 0 else None
    if m and m.group(1) != "00:00:00:00:00:00":
        return m.group(1)
    return _read(f"/sys/class/net/{iface}/address").lower()


_roles_cache = (0.0, None, None)


async def roles(cfg):
    """-> (ap, upstream) radios as dicts {name, driver, mac}; either may be None."""
    global _roles_cache
    if time.time() - _roles_cache[0] < 20:
        return _roles_cache[1], _roles_cache[2]
    radios = []
    for path in sorted(glob.glob("/sys/class/net/*/wireless")):
        name = path.split("/")[4]
        drv_link = f"/sys/class/net/{name}/device/driver"
        driver = os.path.basename(os.path.realpath(drv_link)) if os.path.exists(drv_link) else ""
        radios.append({"name": name, "driver": driver, "mac": await perm_mac(name)})
    want = cfg["radios"]
    ap = next((r for r in radios if want.get("ap_mac") and r["mac"] == want["ap_mac"].lower()), None)
    if not ap:
        ap = next((r for r in radios if r["driver"] == "brcmfmac"), None)
    up = next((r for r in radios if want.get("upstream_mac") and r["mac"] == want["upstream_mac"].lower()), None)
    if not up:
        up = next((r for r in radios if r is not ap), None)
    _roles_cache = (time.time(), ap, up)
    return ap, up


async def _ip4(dev):
    rc, out, _ = await run("nmcli", "-g", "IP4.ADDRESS", "device", "show", dev, timeout=8)
    return out.strip().split(" | ")[0] if rc == 0 else ""


async def status(cfg):
    ap, up = await roles(cfg)
    rc, out, err = await run("nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", timeout=8)
    if rc != 0:
        return {"error": (err or out).strip() or "NetworkManager is not available"}
    devs = {}
    for line in out.splitlines():
        f = split_terse(line)
        if len(f) >= 4:
            devs[f[0]] = {"type": f[1], "state": f[2], "connection": f[3]}

    default_dev = ""
    rc, rout, _ = await run("ip", "route", "show", "default", timeout=5)
    m = re.search(r"\bdev (\S+)", rout)
    if m:
        default_dev = m.group(1)

    res = {"internet_via": default_dev, "upstream": None, "hotspot": None, "ethernet": []}
    if up:
        d = devs.get(up["name"], {})
        info = {"iface": up["name"], "driver": up["driver"], "state": d.get("state", "missing"),
                "connection": d.get("connection", ""), "ssid": "", "signal": None, "ip": ""}
        if d.get("state") == "connected":
            info["ip"] = await _ip4(up["name"])
            rc, wout, _ = await run("nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,CHAN", "device", "wifi",
                                    "list", "ifname", up["name"], "--rescan", "no", timeout=10)
            for line in wout.splitlines():
                f = split_terse(line)
                if len(f) >= 4 and f[0] == "*":
                    info["ssid"], info["signal"], info["channel"] = f[1], int(f[2] or 0), f[3]
                    break
        res["upstream"] = info
    if ap:
        d = devs.get(ap["name"], {})
        res["hotspot"] = {"iface": ap["name"], "driver": ap["driver"], "state": d.get("state", "missing"),
                          "active": d.get("connection") == C.HOTSPOT_CONN}
    for name, d in devs.items():
        if d["type"] == "ethernet":
            res["ethernet"].append({"iface": name, "state": d["state"], "connection": d["connection"],
                                    "ip": await _ip4(name) if d["state"] == "connected" else ""})
    return res


async def scan(cfg):
    _, up = await roles(cfg)
    if not up:
        raise RuntimeError("No second Wi-Fi radio found for the upstream connection. Is the USB Wi-Fi dongle plugged in?")
    args = ["nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY,CHAN", "device", "wifi", "list", "ifname", up["name"]]
    rc, out, err = await run(*args, "--rescan", "yes", timeout=30)
    if rc != 0:  # "scanning not allowed immediately following previous scan" etc.
        rc, out, err = await run(*args, "--rescan", "auto", timeout=30)
    if rc != 0:
        raise RuntimeError((err or out).strip())
    own = await hotspot_get()
    best = {}
    for line in out.splitlines():
        f = split_terse(line)
        if len(f) < 5 or not f[1] or f[1] == own.get("ssid"):
            continue
        net = {"ssid": f[1], "signal": int(f[2] or 0), "security": f[3] or "", "channel": f[4],
               "in_use": f[0] == "*", "open": f[3] in ("", "--"), "enterprise": "802.1X" in (f[3] or "")}
        cur = best.get(net["ssid"])
        if not cur or net["signal"] > cur["signal"] or net["in_use"]:
            net["in_use"] = net["in_use"] or bool(cur and cur["in_use"])
            best[net["ssid"]] = net
    return sorted(best.values(), key=lambda n: (-n["in_use"], -n["signal"]))


async def _bind_to_upstream(uuid, up):
    """Pin a client profile to the USB radio so it never grabs the hotspot radio."""
    await run("nmcli", "connection", "modify", uuid,
              "connection.interface-name", "",
              "802-11-wireless.mac-address", up["mac"],
              "connection.autoconnect", "yes",
              "connection.autoconnect-priority", "10", timeout=10)


async def connect(cfg, ssid, password="", hidden=False):
    _, up = await roles(cfg)
    if not up:
        raise RuntimeError("No upstream Wi-Fi radio found. Plug in the USB Wi-Fi dongle.")
    args = ["nmcli", "--wait", "45", "device", "wifi", "connect", ssid]
    if password:
        args += ["password", password]
    args += ["ifname", up["name"]]
    if hidden:
        args += ["hidden", "yes"]
    rc, out, err = await run(*args, timeout=60)
    if rc != 0:
        msg = (err or out).strip().replace("Error: ", "")
        raise RuntimeError(msg or "Could not connect")
    m = re.search(r"'([0-9a-f]{8}-[0-9a-f-]{27})'", out)
    if m:
        await _bind_to_upstream(m.group(1), up)
    return f"Connected to {ssid}"


EAP_METHODS = {"peap": {"mschapv2", "gtc"}, "ttls": {"mschapv2", "pap"}}


def _friendly(msg):
    m = msg.lower()
    if "secrets were required" in m or "no secrets" in m or "802.1x supplicant failed" in m:
        return "The network turned down that username or password (or the sign-in method). Check them with IT."
    if "certificate" in m or "domain" in m:
        return "The network's certificate didn't match the domain you entered. Check the domain with IT, or leave it blank."
    return msg


async def connect_enterprise(cfg, ssid, identity, password, eap="peap", phase2="mschapv2",
                             domain="", anonymous="", hidden=False):
    """Join a WPA2/WPA3-Enterprise (802.1X) network with a username and password."""
    _, up = await roles(cfg)
    if not up:
        raise RuntimeError("No upstream Wi-Fi radio found. Plug in the USB Wi-Fi dongle.")
    ssid, identity, domain, anonymous = ssid.strip(), identity.strip(), domain.strip().lower(), anonymous.strip()
    if not 1 <= len(ssid.encode()) <= 32:
        raise ValueError("Network name must be 1-32 characters")
    if not identity or not password:
        raise ValueError("Enter your username and password")
    if eap not in EAP_METHODS or phase2 not in EAP_METHODS[eap]:
        raise ValueError("Unsupported sign-in method")
    if domain and not re.fullmatch(r"[a-z0-9.-]{1,253}", domain):
        raise ValueError("The certificate domain should look like radius.example.com")
    if ssid == C.HOTSPOT_CONN:
        raise ValueError("That name is reserved for PiShield's own hotspot")
    name = ssid
    # replace an older profile for the same network instead of piling up duplicates
    await run("nmcli", "connection", "delete", "id", name, timeout=15)
    args = ["nmcli", "connection", "add", "type", "wifi", "con-name", name, "ifname", up["name"], "ssid", ssid,
            "wifi-sec.key-mgmt", "wpa-eap",
            "802-1x.eap", eap, "802-1x.phase2-auth", phase2,
            "802-1x.identity", identity, "802-1x.password", password,
            "connection.autoconnect", "no"]
    if anonymous:
        args += ["802-1x.anonymous-identity", anonymous]
    if domain:  # check the network's certificate against the system CA list + this domain
        args += ["802-1x.system-ca-certs", "yes", "802-1x.domain-suffix-match", domain]
    if hidden:
        args += ["802-11-wireless.hidden", "yes"]
    rc, out, err = await run(*args, timeout=20)
    if rc != 0:
        raise RuntimeError((err or out).strip().replace("Error: ", "") or "Could not save the network")
    m = re.search(r"\(([0-9a-f]{8}-[0-9a-f-]{27})\)", out)
    uuid = m.group(1) if m else name
    await _bind_to_upstream(uuid, up)
    rc, out, err = await run("nmcli", "--wait", "45", "connection", "up", uuid, "ifname", up["name"], timeout=60)
    if rc != 0:
        # don't leave a failing profile retrying in the background: repeated bad logins can lock the account
        await run("nmcli", "connection", "delete", uuid, timeout=15)
        raise RuntimeError(_friendly((err or out).strip().replace("Error: ", "")) or "Could not connect")
    return f"Connected to {ssid}" + ("" if domain else " (certificate not checked)")


async def saved():
    rc, out, _ = await run("nmcli", "-t", "-f", "NAME,UUID,TYPE,DEVICE,AUTOCONNECT-PRIORITY", "connection", "show", timeout=8)
    res = []
    for line in out.splitlines():
        f = split_terse(line)
        if len(f) >= 5 and f[2] == "802-11-wireless" and f[0] != C.HOTSPOT_CONN:
            res.append({"name": f[0], "uuid": f[1], "active": bool(f[3]), "priority": f[4]})
    return res


async def activate(cfg, uuid):
    _, up = await roles(cfg)
    args = ["nmcli", "--wait", "45", "connection", "up", uuid]
    if up:
        await _bind_to_upstream(uuid, up)
        args += ["ifname", up["name"]]
    rc, out, err = await run(*args, timeout=60)
    if rc != 0:
        raise RuntimeError((err or out).strip().replace("Error: ", ""))


async def forget(uuid):
    rc, out, err = await run("nmcli", "connection", "delete", uuid, timeout=15)
    if rc != 0:
        raise RuntimeError((err or out).strip())


async def _nm_field(field, secret=False):
    args = ["nmcli"] + (["-s"] if secret else []) + ["-g", field, "connection", "show", C.HOTSPOT_CONN]
    rc, out, _ = await run(*args, timeout=8)
    return out.strip() if rc == 0 else ""


async def hotspot_get():
    return {
        "ssid": await _nm_field("802-11-wireless.ssid"),
        "password": await _nm_field("802-11-wireless-security.psk", secret=True),
        "band": await _nm_field("802-11-wireless.band") or "bg",
        "channel": await _nm_field("802-11-wireless.channel") or "6",
        "address": C.HOTSPOT_ADDR,
    }


def validate_hotspot(ssid, password, band, channel):
    if not ssid or len(ssid.encode()) > 32:
        raise ValueError("Network name must be 1-32 characters")
    if not (8 <= len(password) <= 63):
        raise ValueError("Password must be 8-63 characters")
    if band not in ("bg", "a"):
        raise ValueError("Band must be 2.4 GHz or 5 GHz")
    ch = int(channel)
    ok = range(1, 14) if band == "bg" else (36, 40, 44, 48, 149, 153, 157, 161, 165)
    if ch not in ok:
        raise ValueError(f"Channel {ch} is not valid for that band")
    return ch


async def hotspot_set(ssid, password, band, channel):
    ch = validate_hotspot(ssid, password, band, channel)
    rc, out, err = await run(
        "nmcli", "connection", "modify", C.HOTSPOT_CONN,
        "802-11-wireless.ssid", ssid,
        "802-11-wireless-security.psk", password,
        "802-11-wireless.band", band,
        "802-11-wireless.channel", str(ch), timeout=15)
    if rc != 0:
        raise RuntimeError((err or out).strip())

    async def restart():
        await asyncio.sleep(2)  # let the HTTP response reach the browser first
        await run("nmcli", "connection", "up", C.HOTSPOT_CONN, timeout=60)

    asyncio.ensure_future(restart())


def leases():
    res = {}
    for path in glob.glob("/var/lib/NetworkManager/dnsmasq-*.leases"):
        for line in _read(path).splitlines():
            f = line.split()
            if len(f) >= 4:
                res[f[2]] = {"ip": f[2], "mac": f[1], "hostname": "" if f[3] == "*" else f[3],
                             "expires": int(f[0]) if f[0].isdigit() else 0}
    return res


def health():
    h = {"temp_c": None, "uptime_s": None, "mem_used_pct": None, "load": None}
    t = _read("/sys/class/thermal/thermal_zone0/temp")
    if t.isdigit():
        h["temp_c"] = round(int(t) / 1000, 1)
    up = _read("/proc/uptime")
    if up:
        h["uptime_s"] = int(float(up.split()[0]))
    mem = {}
    for line in _read("/proc/meminfo").splitlines():
        k, _, v = line.partition(":")
        mem[k] = int(v.split()[0]) if v.split() else 0
    if mem.get("MemTotal"):
        h["mem_used_pct"] = round(100 * (1 - mem.get("MemAvailable", 0) / mem["MemTotal"]), 1)
    try:
        h["load"] = round(os.getloadavg()[0], 2)
    except OSError:
        pass
    return h


async def system_action(action):
    cmd = {"reboot": ["systemctl", "reboot"], "shutdown": ["systemctl", "poweroff"],
           "restart": ["systemctl", "restart", "pishield"]}[action]

    async def later():
        await asyncio.sleep(1.5)
        await run(*cmd, timeout=30)

    asyncio.ensure_future(later())
