"""PiShield's own nftables table. It only adds rules; NetworkManager's NAT rules are untouched."""
import asyncio
import logging
import shutil

from . import config as C

log = logging.getLogger("pishield.firewall")


def ruleset(cfg):
    net, addr = C.HOTSPOT_NET, C.HOTSPOT_ADDR
    lines = [
        "table inet pishield {}",
        "delete table inet pishield",
        "table inet pishield {",
        "  chain input {",
        "    type filter hook input priority filter; policy accept;",
        "    iif lo accept",
        f"    ip saddr {net} accept",
        # nobody on the upstream network may use the Pi as a DNS server
        "    udp dport 53 drop",
        "    tcp dport 53 drop",
    ]
    if not cfg["dashboard_from_upstream"]:
        lines.append(f"    tcp dport {C.HTTP_PORT} drop")
    lines.append("  }")
    if cfg["force_dns"]:
        lines += [
            "  chain prerouting {",
            "    type nat hook prerouting priority dstnat; policy accept;",
            # devices with hard-coded DNS (8.8.8.8 etc.) get answered by PiShield anyway
            f"    ip saddr {net} ip daddr != {addr} udp dport 53 dnat ip to {addr}:53",
            f"    ip saddr {net} ip daddr != {addr} tcp dport 53 dnat ip to {addr}:53",
            "  }",
            "  chain forward {",
            "    type filter hook forward priority filter; policy accept;",
            # DNS-over-TLS would skip PiShield entirely
            f"    ip saddr {net} tcp dport 853 reject with tcp reset",
            f"    ip saddr {net} udp dport 853 reject",
            "  }",
        ]
    lines.append("}")
    return "\n".join(lines) + "\n"


async def _run(*args):
    p = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, err = await p.communicate()
    return p.returncode, err.decode(errors="replace").strip()


DOCKER_RULES = [
    ["-s", C.HOTSPOT_NET, "-j", "ACCEPT"],
    ["-d", C.HOTSPOT_NET, "-m", "conntrack", "--ctstate", "RELATED,ESTABLISHED", "-j", "ACCEPT"],
]


async def docker_fix():
    """Docker sets the FORWARD policy to DROP, which cuts hotspot devices off the internet.
    Let hotspot traffic through via the DOCKER-USER chain, which Docker leaves alone."""
    if C.DEV or not shutil.which("iptables"):
        return
    if (await _run("iptables", "-n", "-L", "DOCKER-USER"))[0] != 0:
        return  # Docker isn't running
    for rule in reversed(DOCKER_RULES):
        if (await _run("iptables", "-C", "DOCKER-USER", *rule))[0] != 0:
            rc, err = await _run("iptables", "-I", "DOCKER-USER", *rule)
            if rc:
                log.error("could not add Docker forwarding rule: %s", err)
            else:
                log.info("let hotspot traffic through Docker's firewall")


async def apply(cfg):
    if C.DEV or not shutil.which("nft"):
        log.info("firewall: skipped (dev mode or nft missing)")
        return
    p = await asyncio.create_subprocess_exec(
        "nft", "-f", "-", stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    _, err = await p.communicate(ruleset(cfg).encode())
    if p.returncode != 0:
        log.error("nft failed: %s", err.decode().strip())
    else:
        log.info("firewall rules applied (force_dns=%s)", cfg["force_dns"])
