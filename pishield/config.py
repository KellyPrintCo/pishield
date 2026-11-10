"""Persistent configuration (JSON on disk) and admin password hashing."""
import copy
import hashlib
import hmac
import json
import os
import secrets
import threading

DATA_DIR = os.environ.get("PISHIELD_DATA", "/var/lib/pishield")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
LISTS_DIR = os.path.join(DATA_DIR, "lists")

# Dev mode: no firewall changes, every client allowed, odd ports OK.
DEV = os.environ.get("PISHIELD_DEV") == "1"
DNS_PORT = int(os.environ.get("PISHIELD_DNS_PORT", "53"))
HTTP_PORT = int(os.environ.get("PISHIELD_HTTP_PORT", "80"))
BIND = os.environ.get("PISHIELD_BIND", "0.0.0.0")

# The hotspot side. install.sh uses the same values.
HOTSPOT_CONN = "pishield-hotspot"
HOTSPOT_ADDR = "192.168.50.1"
HOTSPOT_NET = "192.168.50.0/24"

DEFAULT_LISTS = [
    {
        "id": "stevenblack",
        "name": "StevenBlack Unified (ads + malware)",
        "url": "https://raw.githubusercontent.com/StevenBlack/hosts/master/hosts",
        "enabled": True,
    },
    {
        "id": "adguard-dns",
        "name": "AdGuard DNS filter",
        "url": "https://adguardteam.github.io/AdGuardSDNSFilter/Filters/filter.txt",
        "enabled": True,
    },
    {
        "id": "peterlowe",
        "name": "Peter Lowe's ad & tracking servers",
        "url": "https://pgl.yoyo.org/adservers/serverlist.php?hostformat=hosts&showintro=0&mimetype=plaintext",
        "enabled": False,
    },
]

DEFAULTS = {
    # off | sites | everywhere
    "mode": "sites",
    # [{"domain": "cnn.com", "enabled": true, "extra": ["ads.example.net"], "added": 0}]
    "sites": [],
    # how long (seconds) a device counts as "on" a selected site after its last lookup
    "site_window": 180,
    "allowlist": [],
    "blocklist": [],
    "lists": DEFAULT_LISTS,
    "lists_updated": 0,
    # "auto" = use the DNS servers of whatever network the Pi joined (keeps captive portals working)
    "upstream_dns": "auto",
    # redirect all hotspot DNS to the Pi and switch off browser/iCloud encrypted-DNS bypasses
    "force_dns": True,
    "dashboard_from_upstream": False,
    # permanent MACs of the two radios, written by install.sh
    "radios": {"ap_mac": "", "upstream_mac": ""},
    "admin_hash": "",
}


class Config:
    def __init__(self, path=CONFIG_PATH):
        self.path = path
        self._lock = threading.RLock()
        self.data = copy.deepcopy(DEFAULTS)
        self.load()

    def load(self):
        try:
            with open(self.path) as f:
                disk = json.load(f)
        except FileNotFoundError:
            disk = {}
        with self._lock:
            for k, v in disk.items():
                self.data[k] = v
            for k, v in DEFAULTS.items():
                self.data.setdefault(k, copy.deepcopy(v))

    def save(self):
        with self._lock:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.data, f, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)

    def __getitem__(self, k):
        return self.data[k]

    def __setitem__(self, k, v):
        self.data[k] = v

    def get(self, k, default=None):
        return self.data.get(k, default)


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${h.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        algo, salt, digest = stored.split("$")
        if algo != "scrypt":
            return False
        h = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1, dklen=32)
        return hmac.compare_digest(h.hex(), digest)
    except Exception:
        return False
