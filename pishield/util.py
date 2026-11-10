"""Domain helpers."""
import re

DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?\.)+[a-z0-9-]{2,63}$"
)

# Two-label public suffixes common enough to matter for "which site is this?"
MULTI_SUFFIXES = {
    "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "ltd.uk", "plc.uk", "net.uk",
    "com.au", "net.au", "org.au", "edu.au", "gov.au", "co.nz", "org.nz", "govt.nz",
    "co.jp", "ne.jp", "or.jp", "com.br", "com.mx", "com.ar", "com.co", "com.tr",
    "co.in", "net.in", "org.in", "co.za", "com.cn", "com.hk", "com.sg", "com.tw",
    "co.kr", "co.il", "com.my", "com.ph", "com.pk", "com.sa", "com.eg", "com.ng",
    "co.id", "co.th", "com.vn", "com.ua", "com.pl", "com.es", "com.pt", "co.at",
}


def normalize_domain(s: str, strip_www: bool = False) -> str:
    """Turn 'https://www.CNN.com/world?x=1' into 'cnn.com' (with strip_www) or ''. """
    s = (s or "").strip().lower()
    if "://" in s:
        s = s.split("://", 1)[1]
    for sep in "/?#":
        s = s.split(sep, 1)[0]
    if "@" in s:
        s = s.rsplit("@", 1)[1]
    s = s.split(":", 1)[0].strip(".")
    if s.startswith("*."):
        s = s[2:]
    if s.startswith("||"):
        s = s[2:].rstrip("^")
    if strip_www and s.startswith("www.") and s.count(".") >= 2:
        s = s[4:]
    return s if DOMAIN_RE.match(s) else ""


def normalize_list(items, strip_www=False):
    out, seen = [], set()
    for raw in items:
        d = normalize_domain(raw, strip_www)
        if d and d not in seen:
            seen.add(d)
            out.append(d)
    return out


def suffix_match(name: str, domains):
    """Return the entry in `domains` that `name` equals or is a subdomain of."""
    if not domains:
        return None
    while True:
        if name in domains:
            return name
        i = name.find(".")
        if i < 0:
            return None
        name = name[i + 1:]


def registrable(name: str) -> str:
    """Best-effort 'site' for a hostname: static.cnn.com -> cnn.com, a.b.co.uk -> b.co.uk."""
    parts = name.split(".")
    if len(parts) <= 2:
        return name
    if ".".join(parts[-2:]) in MULTI_SUFFIXES:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])
