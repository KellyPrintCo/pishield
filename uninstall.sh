#!/usr/bin/env bash
# Removes PiShield. Add --purge to also delete settings and downloaded lists.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run with sudo: sudo ./uninstall.sh [--purge]"; exit 1; }

systemctl disable --now pishield 2>/dev/null || true
rm -f /etc/systemd/system/pishield.service
systemctl daemon-reload
nft delete table inet pishield 2>/dev/null || true
if command -v iptables >/dev/null; then  # rules PiShield added for Docker hosts
  iptables -D DOCKER-USER -s 192.168.50.0/24 -j ACCEPT 2>/dev/null || true
  iptables -D DOCKER-USER -d 192.168.50.0/24 -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT 2>/dev/null || true
fi
nmcli connection delete pishield-hotspot 2>/dev/null || true
rm -f /etc/NetworkManager/dnsmasq-shared.d/pishield.conf
rm -rf /opt/pishield
if [[ "${1:-}" == "--purge" ]]; then rm -rf /var/lib/pishield; fi
echo "PiShield removed.${1:+ Settings deleted.}"
echo "Saved Wi-Fi networks are still pinned to the USB dongle; edit them with nmtui if needed."
