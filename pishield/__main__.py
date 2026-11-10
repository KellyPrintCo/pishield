"""Entry point.

    python -m pishield                 run the service
    python -m pishield passwd          set the dashboard password (prompt, or one line on stdin)
    python -m pishield init --ap-mac M --upstream-mac M   (used by install.sh; password from PISHIELD_INIT_PASSWORD)
"""
import argparse
import asyncio
import getpass
import logging
import os
import signal
import sys

from . import config as C
from .config import Config, hash_password

log = logging.getLogger("pishield")


async def serve():
    from aiohttp import web

    from . import firewall
    from .blocklists import Blocklists
    from .dnsserver import Resolver, Stats, UDPServer, tcp_handler
    from .web import create_app

    cfg = Config()
    if not cfg["admin_hash"]:
        pw = os.environ.get("PISHIELD_ADMIN_PASSWORD", "pishield")
        cfg["admin_hash"] = hash_password(pw)
        cfg.save()
        log.warning("no dashboard password was set; using %r - change it in Settings", pw)

    lists = Blocklists(cfg)
    await asyncio.to_thread(lists.rebuild)
    stats = Stats()
    resolver = Resolver(cfg, lists, stats)

    loop = asyncio.get_running_loop()
    await loop.create_datagram_endpoint(lambda: UDPServer(resolver), local_addr=(C.BIND, C.DNS_PORT))
    await asyncio.start_server(tcp_handler(resolver), C.BIND, C.DNS_PORT)
    log.info("DNS listening on %s:%d", C.BIND, C.DNS_PORT)

    await firewall.apply(cfg)

    app = create_app({"cfg": cfg, "lists": lists, "resolver": resolver, "stats": stats})
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, C.BIND, C.HTTP_PORT).start()
    log.info("dashboard on http://%s:%d", C.HOTSPOT_ADDR if not C.DEV else "127.0.0.1", C.HTTP_PORT)

    async def housekeeping():
        n = 0
        while True:
            if n % 5 == 0:  # Docker may start (and reset its rules) after PiShield
                await firewall.docker_fix()
            n += 1
            await asyncio.sleep(60)
            resolver.prune()

    tasks = [asyncio.ensure_future(lists.auto_update_loop()), asyncio.ensure_future(housekeeping())]
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    await stop.wait()
    for t in tasks:
        t.cancel()
    await runner.cleanup()


def main():
    logging.basicConfig(level=os.environ.get("PISHIELD_LOG", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(prog="pishield")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("passwd")
    init = sub.add_parser("init")
    init.add_argument("--ap-mac", default="")
    init.add_argument("--upstream-mac", default="")
    args = ap.parse_args()

    if args.cmd == "passwd":
        pw = getpass.getpass("New dashboard password: ") if sys.stdin.isatty() else sys.stdin.readline().strip()
        if len(pw) < 6:
            sys.exit("Password must be at least 6 characters")
        cfg = Config()
        cfg["admin_hash"] = hash_password(pw)
        cfg.save()
        print("Password updated. Restart not needed for new logins.")
    elif args.cmd == "init":
        cfg = Config()
        cfg["radios"] = {"ap_mac": args.ap_mac.lower(), "upstream_mac": args.upstream_mac.lower()}
        pw = os.environ.get("PISHIELD_INIT_PASSWORD")
        if pw:
            cfg["admin_hash"] = hash_password(pw)
        cfg.save()
        print(f"Config written to {C.CONFIG_PATH}")
    else:
        asyncio.run(serve())


if __name__ == "__main__":
    main()
