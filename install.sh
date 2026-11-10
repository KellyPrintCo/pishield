#!/usr/bin/env bash
# PiShield installer for Raspberry Pi OS (Bookworm or newer, NetworkManager).
#
#   sudo ./install.sh --check   look for clashes with other things on this Pi. Changes nothing.
#   sudo ./install.sh           install. Runs the same check first and asks before changing anything.
#
# Optional, for unattended installs:
#   PISHIELD_SSID  PISHIELD_WIFI_PASSWORD  PISHIELD_ADMIN_PASSWORD  PISHIELD_COUNTRY
#   PISHIELD_HTTP_PORT   dashboard port (default 80, or a free one if something else has 80)
#   PISHIELD_YES=1       don't ask "Continue?"
#   AP_IFACE / UPSTREAM_IFACE   force which radio broadcasts the hotspot / joins networks
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP=/opt/pishield
DATA=/var/lib/pishield
UNIT=/etc/systemd/system/pishield.service
CONN=pishield-hotspot
ADDR=192.168.50.1
PACKAGES=(python3-venv python3-pip iw ethtool nftables rfkill dnsmasq-base)

MODE=install
case "${1:-}" in
  "") ;;
  --check|check) MODE=check ;;
  -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
  *) echo "Unknown option: $1   (use --check to look for problems without installing)"; exit 2 ;;
esac

say()  { printf '\n\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!! \033[0m%s\n' "$*"; WARNINGS=$((WARNINGS + 1)); }
ok()   { printf '\033[1;32mok \033[0m%s\n' "$*"; }
info() { printf '   %s\n' "$*"; }
block(){ printf '\033[1;31mxx \033[0m%s\n' "$*"; BLOCKERS=$((BLOCKERS + 1)); }
die()  { printf '\033[1;31mxx \033[0m%s\n' "$*" >&2; exit 1; }
tty_ok() { [[ -t 0 ]]; }
have() { command -v "$1" >/dev/null 2>&1; }
BLOCKERS=0
WARNINGS=0

[[ -f "$SRC/pishield/__main__.py" ]] || die "Run this from the pishield folder."
if [[ $MODE == install && $EUID -ne 0 ]]; then
  die "Run with sudo:  sudo ./install.sh     (or: sudo ./install.sh --check  to only look for problems)"
fi
ROOT=0; [[ $EUID -eq 0 ]] && ROOT=1

# ---------------------------------------------------------------- helpers
driver_of() { basename "$(readlink -f "/sys/class/net/$1/device/driver" 2>/dev/null)" 2>/dev/null || true; }
perm_mac()  {
  local m=""
  have ethtool && m=$(ethtool -P "$1" 2>/dev/null | awk '{print tolower($3)}')
  if [[ -n "$m" && "$m" != "00:00:00:00:00:00" ]]; then echo "$m"; else tr 'A-F' 'a-f' < "/sys/class/net/$1/address"; fi
}
# 0 = can run a hotspot, 1 = can't, 2 = unknown (iw not installed yet)
supports_ap() {
  have iw || return 2
  local phy; phy=$(basename "$(readlink -f "/sys/class/net/$1/phy80211")")
  iw phy "$phy" info 2>/dev/null | sed -n '/Supported interface modes/,/^[[:space:]]*[A-Z]/p' | grep -q '\* AP$'
}
ap_word() { local rc=0; supports_ap "$1" || rc=$?; case $rc in 0) echo yes ;; 1) echo no ;; *) echo unknown ;; esac; }

pick_radios() {
  radios=()
  local d r rc
  for d in /sys/class/net/*; do
    if [[ -e $d/phy80211 ]]; then radios+=("$(basename "$d")"); fi
  done
  AP_IF=${AP_IFACE:-}
  UP_IF=${UPSTREAM_IFACE:-}
  AP_STATE=yes
  (( ${#radios[@]} > 0 )) || { AP_STATE=none; return 0; }
  if [[ -z "$AP_IF" ]]; then
    for r in "${radios[@]}"; do if [[ "$(driver_of "$r")" == brcmfmac ]]; then AP_IF=$r; fi; done
    [[ -n "$AP_IF" ]] || AP_IF=${radios[0]}
  fi
  if [[ -z "$UP_IF" ]]; then
    for r in "${radios[@]}"; do if [[ "$r" != "$AP_IF" ]]; then UP_IF=$r; break; fi; done
  fi
  rc=0; supports_ap "$AP_IF" || rc=$?
  if (( rc == 1 )); then
    if [[ -n "$UP_IF" ]] && supports_ap "$UP_IF"; then
      r=$AP_IF; AP_IF=$UP_IF; UP_IF=$r; AP_STATE=swapped
    else
      AP_STATE=no
    fi
  elif (( rc == 2 )); then
    AP_STATE=unknown
  fi
  return 0
}

PISH_PID=0
if have systemctl; then PISH_PID=$(systemctl show -p MainPID --value pishield 2>/dev/null || echo 0); fi
[[ -n "$PISH_PID" ]] || PISH_PID=0

# Lines from `ss` for listeners on a port that aren't PiShield itself.
port_others() {
  local line
  while IFS= read -r line; do
    [[ -n "$line" ]] || continue
    if [[ "$PISH_PID" != 0 && "$line" == *"pid=$PISH_PID,"* ]]; then continue; fi
    printf '%s\n' "$line"
  done < <(ss -Hlntup "sport = :$1" 2>/dev/null || true)
}
port_free() { [[ -z "$(port_others "$1")" ]]; }
# "dnsmasq, lighttpd" from ss lines
port_names() {
  local n
  n=$(port_others "$1" | grep -o 'users:(("[^"]*"' | sed 's/users:(("//; s/"$//' | sort -u | paste -sd, - | sed 's/,/, /g' || true)
  echo "${n:-a program (run with sudo to see which)}"
}

wifi_client_profiles() {  # "uuid<TAB>name" for saved Wi-Fi networks that would move to the dongle
  local uuid name mode
  while IFS= read -r uuid; do
    [[ -n "$uuid" ]] || continue
    [[ "$(nmcli -g connection.type connection show "$uuid" 2>/dev/null || true)" == 802-11-wireless ]] || continue
    name=$(nmcli -g connection.id connection show "$uuid" 2>/dev/null || true)
    mode=$(nmcli -g 802-11-wireless.mode connection show "$uuid" 2>/dev/null || true)
    [[ "$name" == "$CONN" || "$mode" == ap ]] && continue
    printf '%s\t%s\n' "$uuid" "$name"
  done < <(nmcli -g UUID connection show 2>/dev/null)
}

# ---------------------------------------------------------------- pre-flight check (changes nothing)
preflight() {
  say "Checking this Pi for anything PiShield could clash with (nothing is changed yet)"
  (( ROOT )) || info "(not running as root: some checks are skipped. Use: sudo ./install.sh --check)"

  # system
  if ! have nmcli || ! systemctl is-active --quiet NetworkManager 2>/dev/null; then
    block "NetworkManager isn't running. PiShield needs Raspberry Pi OS Bookworm or newer."
    return 0
  fi
  ok "NetworkManager is running"
  if [[ "$PISH_PID" != 0 || -f "$UNIT" ]]; then
    info "PiShield is already installed: this will update it and keep its settings."
  fi

  # radios
  pick_radios
  case $AP_STATE in
    none)    block "No Wi-Fi hardware found."; return 0 ;;
    no)      block "$AP_IF can't run a hotspot (AP mode), and there's no other radio that can." ;;
    unknown) info "Hotspot support will be checked after 'iw' is installed." ;;
    swapped) warn "The built-in Wi-Fi can't run a hotspot here, so the USB dongle ($AP_IF) will broadcast it instead." ;;
  esac
  for r in "${radios[@]}"; do
    info "$(printf '%-8s driver=%-12s hotspot-capable=%s' "$r" "$(driver_of "$r")" "$(ap_word "$r")")"
  done
  if [[ $AP_STATE != no ]]; then
    ok "Hotspot radio: $AP_IF ($(driver_of "$AP_IF"))"
    if [[ -n "$UP_IF" ]]; then ok "Internet radio: $UP_IF ($(driver_of "$UP_IF"))"; fi
  fi

  # what the hotspot radio is doing right now
  local cur ip4 method eth
  cur=$(nmcli -g GENERAL.CONNECTION device show "$AP_IF" 2>/dev/null || true)
  eth=$(nmcli -t -f TYPE,STATE device 2>/dev/null | grep -c '^ethernet:connected' || true)
  if [[ -n "$cur" && "$cur" != "$CONN" ]]; then
    ip4=$(nmcli -g IP4.ADDRESS device show "$AP_IF" 2>/dev/null | head -1 | cut -d'|' -f1 | tr -d ' ' || true)
    if [[ -n "$UP_IF" ]]; then
      warn "The Pi is on Wi-Fi '$cur' through $AP_IF${ip4:+ ($ip4)}. It will move to the USB dongle ($UP_IF)"
      info "and most likely get a different IP address. Anything that reaches this Pi by IP"
      info "(bookmarks, port forwards, other devices' settings) needs the new one, or reserve it in your router."
      method=$(nmcli -g ipv4.method connection show "$cur" 2>/dev/null || true)
      [[ "$method" == manual ]] && info "'$cur' has a static IP; it moves to the dongle with the network."
    elif (( eth == 0 )); then
      block "The Pi's only network is '$cur' on $AP_IF, which becomes the hotspot. Plug in the USB dongle or Ethernet first."
    else
      warn "Wi-Fi '$cur' on $AP_IF will be dropped (no USB dongle found). The Pi will use Ethernet for internet."
    fi
    if ss -Htn state established '( sport = :22 )' 2>/dev/null | awk '{print $3}' | grep -q "^${ip4%/*}:"; then
      warn "Your SSH session comes in over $AP_IF. It will drop at the end of the install."
      info "Reconnect by joining the PiShield Wi-Fi (ssh to $ADDR), or over Ethernet."
    fi
  fi
  if [[ -n "$UP_IF" ]]; then
    local moving
    moving=$(wifi_client_profiles | cut -f2 | paste -sd, - | sed 's/,/, /g' || true)
    [[ -n "$moving" ]] && info "Saved Wi-Fi networks that will use the USB dongle from now on: $moving"
  fi
  if grep -rqsw "$AP_IF" /etc/systemd/system /etc/cron.d /etc/rc.local 2>/dev/null; then
    warn "Something in /etc/systemd/system, cron or rc.local mentions $AP_IF by name; it may stop working."
  fi

  # ports
  local existing
  existing=$(sed -n 's/^Environment=PISHIELD_HTTP_PORT=//p' "$UNIT" 2>/dev/null | head -1 || true)
  HTTP_PORT=${PISHIELD_HTTP_PORT:-${existing:-80}}
  if (( ! ROOT )) && [[ "$PISH_PID" != 0 ]]; then
    info "Port check skipped: PiShield is running and only sudo can tell its ports from others'."
  elif ! port_free 53; then
    block "Port 53 (DNS) is already used by $(port_names 53)."
    info "Only one DNS server can run. Stop/remove it first (Pi-hole, AdGuard Home, dnsmasq, bind9...)."
  else
    ok "Port 53 (DNS) is free"
  fi
  if (( ! ROOT )) && [[ "$PISH_PID" != 0 ]]; then
    :
  elif ! port_free "$HTTP_PORT"; then
    if [[ -n "${PISHIELD_HTTP_PORT:-}" ]]; then
      block "Port $HTTP_PORT (PISHIELD_HTTP_PORT) is already used by $(port_names "$HTTP_PORT")."
    else
      local who p old=$HTTP_PORT; who=$(port_names "$HTTP_PORT")
      for p in 8080 8081 8088 8090 8888 9080; do if port_free "$p"; then HTTP_PORT=$p; break; fi; done
      if [[ "$HTTP_PORT" == "$old" ]]; then
        block "Port $old is used by $who and no spare port was found. Set PISHIELD_HTTP_PORT=<free port>."
      else
        ok "Port $old is used by $who, so the dashboard will use port $HTTP_PORT instead"
      fi
    fi
  else
    ok "Port $HTTP_PORT (dashboard) is free"
  fi

  # other routers / firewalls on this Pi
  local uuid
  while IFS= read -r uuid; do
    [[ -n "$uuid" ]] || continue
    local n m; n=$(nmcli -g connection.id connection show "$uuid" 2>/dev/null || true)
    m=$(nmcli -g ipv4.method connection show "$uuid" 2>/dev/null || true)
    if [[ "$m" == shared && "$n" != "$CONN" ]]; then
      warn "'$n' also shares this Pi's internet (NetworkManager 'shared'). PiShield's DHCP settings"
      info "would apply to it too, and its devices would get no DNS. Remove it or point it elsewhere."
    fi
  done < <(nmcli -g UUID connection show 2>/dev/null)
  if (( ROOT )); then
    if have ufw && ufw status 2>/dev/null | grep -q '^Status: active'; then
      warn "ufw is on and may block hotspot devices. After installing, run:"
      info "sudo ufw allow in on $AP_IF   and   sudo ufw route allow in on $AP_IF"
    fi
    if have iptables && iptables -n -L DOCKER-USER >/dev/null 2>&1; then
      ok "Docker found: PiShield adds two rules to DOCKER-USER so hotspot devices can reach the internet"
    fi
  fi
  if systemctl is-active --quiet firewalld 2>/dev/null; then
    warn "firewalld is running and may block the hotspot. Add $AP_IF to its 'trusted' zone after installing."
  fi
  if systemctl is-active --quiet hostapd 2>/dev/null; then
    warn "hostapd is running (another hotspot setup?). If it uses $AP_IF, stop and disable it first."
  fi

  # summary of what installing changes
  local url="http://$ADDR"; [[ "$HTTP_PORT" == 80 ]] || url="http://$ADDR:$HTTP_PORT"
  local missing=() pkg
  for pkg in "${PACKAGES[@]}"; do dpkg -s "$pkg" >/dev/null 2>&1 || missing+=("$pkg"); done
  echo
  echo "   Installing will:"
  echo "    - turn $AP_IF into the PiShield hotspot ($ADDR) and leave your other projects' files alone"
  echo "    - run a DNS server on port 53 and the dashboard at $url"
  [[ -n "$UP_IF" ]] && echo "    - pin saved Wi-Fi networks to the USB dongle ($UP_IF)"
  (( ${#missing[@]} )) && echo "    - install packages: ${missing[*]}"
  echo "    - add $APP, $DATA, the 'pishield' service, the nftables table 'pishield',"
  echo "      and /etc/NetworkManager/dnsmasq-shared.d/pishield.conf"
  echo "    - set the Wi-Fi country if it isn't set"
  echo "   Nothing else is changed or removed. 'sudo ./uninstall.sh' undoes it."
  echo
  if (( BLOCKERS )); then
    printf '\033[1;31m   %d problem(s) must be fixed before installing.\033[0m\n' "$BLOCKERS"
  elif (( WARNINGS )); then
    printf '\033[1;33m   No blockers. Read the %d note(s) marked !! above.\033[0m\n' "$WARNINGS"
  else
    printf '\033[1;32m   No problems found.\033[0m\n'
  fi
}

preflight
if [[ $MODE == check ]]; then
  (( BLOCKERS )) && exit 1
  exit 0
fi
(( BLOCKERS == 0 )) || die "Not installing. Fix the problems above, then run this again."
if tty_ok && [[ "${PISHIELD_YES:-}" != 1 ]]; then
  read -rp "Continue with the install? [y/N] " yn
  [[ "$yn" == [yY]* ]] || { echo "Nothing was changed."; exit 0; }
fi

# ---------------------------------------------------------------- packages
say "Installing packages"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${PACKAGES[@]}" >/dev/null

if [[ $AP_STATE == unknown ]]; then  # iw is installed now
  pick_radios
  [[ $AP_STATE == no ]] && die "$AP_IF doesn't support hotspot (AP) mode."
  [[ $AP_STATE == swapped ]] && warn "Swapped roles: $AP_IF will broadcast the hotspot."
fi
AP_MAC=$(perm_mac "$AP_IF")
UP_MAC=""
[[ -n "$UP_IF" ]] && UP_MAC=$(perm_mac "$UP_IF")

systemctl stop pishield 2>/dev/null || true   # re-install: free our own ports

# ---------------------------------------------------------------- wifi country
rfkill unblock wifi || true
COUNTRY_NOW=$(iw reg get 2>/dev/null | awk '/^country/{gsub(":","",$2); print $2; exit}')
if [[ -z "$COUNTRY_NOW" || "$COUNTRY_NOW" == "00" ]]; then
  COUNTRY=${PISHIELD_COUNTRY:-}
  if [[ -z "$COUNTRY" ]] && tty_ok; then read -rp "Two-letter Wi-Fi country code [US]: " COUNTRY; fi
  COUNTRY=${COUNTRY:-US}
  say "Setting Wi-Fi country to ${COUNTRY^^}"
  raspi-config nonint do_wifi_country "${COUNTRY^^}" 2>/dev/null || iw reg set "${COUNTRY^^}"
fi

# ---------------------------------------------------------------- questions
SSID=${PISHIELD_SSID:-}
WIFI_PW=${PISHIELD_WIFI_PASSWORD:-}
ADMIN_PW=${PISHIELD_ADMIN_PASSWORD:-}
gen() { python3 -c "import secrets,string;a=string.ascii_lowercase+string.digits;print(''.join(secrets.choice(a) for _ in range($1)))"; }
if tty_ok; then
  [[ -n "$SSID" ]] || read -rp "Hotspot name [PiShield]: " SSID
  while [[ -z "$WIFI_PW" ]]; do
    read -rsp "Hotspot password (8+ chars, blank = generate one): " WIFI_PW; echo
    [[ -z "$WIFI_PW" ]] && WIFI_PW=$(gen 12) && echo "   Generated: $WIFI_PW"
    (( ${#WIFI_PW} >= 8 )) || { echo "   Too short."; WIFI_PW=""; }
  done
  while [[ -z "$ADMIN_PW" ]]; do
    read -rsp "Dashboard password (6+ chars, blank = generate one): " ADMIN_PW; echo
    [[ -z "$ADMIN_PW" ]] && ADMIN_PW=$(gen 10) && echo "   Generated: $ADMIN_PW"
    (( ${#ADMIN_PW} >= 6 )) || { echo "   Too short."; ADMIN_PW=""; }
  done
fi
SSID=${SSID:-PiShield}
[[ -n "$WIFI_PW" ]] || WIFI_PW=$(gen 12)
[[ -n "$ADMIN_PW" ]] || ADMIN_PW=$(gen 10)

# ---------------------------------------------------------------- app
say "Installing PiShield to $APP"
install -d -m 755 "$APP" "$DATA"
rm -rf "$APP/pishield"
cp -r "$SRC/pishield" "$APP/pishield"
cp "$SRC/requirements.txt" "$APP/"
find "$APP/pishield" -name '__pycache__' -prune -exec rm -rf {} +
[[ -x "$APP/venv/bin/python" ]] || python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q --upgrade pip >/dev/null
"$APP/venv/bin/pip" install -q -r "$APP/requirements.txt"
chmod 700 "$DATA"
( cd "$APP" && PISHIELD_DATA="$DATA" PISHIELD_INIT_PASSWORD="$ADMIN_PW" \
    "$APP/venv/bin/python" -m pishield init --ap-mac "$AP_MAC" --upstream-mac "$UP_MAC" )

# ---------------------------------------------------------------- NetworkManager
say "Configuring the hotspot"
install -d /etc/NetworkManager/dnsmasq-shared.d
cat > /etc/NetworkManager/dnsmasq-shared.d/pishield.conf <<EOF
# PiShield answers DNS itself; NetworkManager's dnsmasq only hands out addresses.
port=0
dhcp-option=option:dns-server,$ADDR
dhcp-option=option:domain-name,lan
dhcp-authoritative
EOF

nmcli connection delete "$CONN" >/dev/null 2>&1 || true
nmcli connection add type wifi con-name "$CONN" ifname "$AP_IF" ssid "$SSID" \
  802-11-wireless.mode ap 802-11-wireless.band bg 802-11-wireless.channel 6 \
  802-11-wireless.mac-address "$AP_MAC" \
  802-11-wireless-security.key-mgmt wpa-psk 802-11-wireless-security.psk "$WIFI_PW" \
  802-11-wireless-security.proto rsn 802-11-wireless-security.pairwise ccmp \
  802-11-wireless-security.group ccmp 802-11-wireless-security.pmf disable \
  ipv4.method shared ipv4.addresses "$ADDR/24" ipv6.method disabled \
  connection.autoconnect yes connection.autoconnect-priority 100 >/dev/null
# match the radio by MAC instead of name (wlan0/wlan1 can swap between boots)
nmcli connection modify "$CONN" connection.interface-name ""

# Existing Wi-Fi client profiles (e.g. the one Raspberry Pi Imager made) move to the USB radio.
if [[ -n "$UP_MAC" ]]; then
  while IFS=$'\t' read -r uuid name; do
    echo "   Moving saved network '$name' to $UP_IF"
    nmcli connection modify "$uuid" connection.interface-name "" 802-11-wireless.mac-address "$UP_MAC" \
      connection.autoconnect yes connection.autoconnect-priority 10
  done < <(wifi_client_profiles)
fi

# ---------------------------------------------------------------- service
say "Starting the PiShield service"
cat > "$UNIT" <<EOF
[Unit]
Description=PiShield ad-blocking hotspot
After=NetworkManager.service
Wants=NetworkManager.service

[Service]
Environment=PISHIELD_DATA=$DATA
Environment=PISHIELD_HTTP_PORT=$HTTP_PORT
WorkingDirectory=$APP
ExecStart=$APP/venv/bin/python -m pishield
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable pishield >/dev/null 2>&1
systemctl restart pishield
sleep 2
systemctl is-active --quiet pishield || { journalctl -u pishield -n 30 --no-pager; die "PiShield failed to start (log above)."; }

cat > "$APP/bring-up.sh" <<EOF
#!/bin/sh
nmcli connection up $CONN || true
${UP_IF:+nmcli device connect $UP_IF || true}
EOF
chmod +x "$APP/bring-up.sh"

URL="http://$ADDR"; [[ "$HTTP_PORT" == 80 ]] || URL="http://$ADDR:$HTTP_PORT"
cat <<EOF

────────────────────────────────────────────────────────────
  PiShield is installed.

  Wi-Fi name : $SSID
  Wi-Fi pass : $WIFI_PW
  Dashboard  : $URL
  Dash pass  : $ADMIN_PW

  1. Join "$SSID" from your phone or laptop.
  2. Open $URL and sign in.
  3. Network tab -> Find networks -> join the Wi-Fi with internet.
  4. Sites tab -> add the websites you want ads blocked on.

  The hotspot starts in a few seconds. If you are connected to
  this Pi over Wi-Fi, your SSH session may drop now — that's
  expected. Reconnect over Ethernet or join "$SSID".
────────────────────────────────────────────────────────────
EOF
systemd-run --quiet --on-active=4 --unit=pishield-bringup "$APP/bring-up.sh" 2>/dev/null \
  || (sleep 4; "$APP/bring-up.sh") &
