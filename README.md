# PiShield

A portable ad-blocking Wi-Fi hotspot for a Raspberry Pi 4.

The Pi joins whatever Wi-Fi has internet (home, hotel, café, phone hotspot) with the
**USB Wi-Fi dongle**, and broadcasts its own **PiShield** network from the **built-in
Wi-Fi**. Everything you connect to PiShield gets ad blocking. From the dashboard you
pick **which websites** to block ads on, or switch to blocking everywhere.

```
 internet ── Wi-Fi you joined ──► [USB dongle]  Raspberry Pi 4  [built-in Wi-Fi] ──► "PiShield" ──► your phone/laptop/TV
                                           DNS ad filter + dashboard at 192.168.50.1
```

## What you need

- Raspberry Pi 4 (8 GB is plenty — PiShield uses ~100 MB)
- microSD card, 8 GB+
- USB Wi-Fi dongle (any that works on Linux in normal client mode)
- USB-C power supply (official 3 A recommended)

## 1. Flash the SD card

In **Raspberry Pi Imager**: Raspberry Pi 4 → **Raspberry Pi OS Lite (64-bit)**.
Click *Edit settings*:

- set a username and password
- set your **Wi-Fi country**
- turn on **SSH**
- (optional) enter your home Wi-Fi, so you can SSH in for the install

Plug the USB dongle in, then boot the Pi.

## 2. Copy PiShield over and install

From this Mac (replace `pi` with your username):

```bash
scp -r ~/Mdev/pishield pi@raspberrypi.local:~
```

```bash
ssh pi@raspberrypi.local
```

On the Pi, first check for clashes with anything else running on it. This
changes nothing:

```bash
cd ~/pishield && sudo ./install.sh --check
```

Then install:

```bash
sudo ./install.sh
```

The installer runs the same check, shows exactly what it will change, and asks
before touching anything. It stops if something would break (another DNS server
on port 53, or the built-in Wi-Fi being the Pi's only connection). Then it asks
for a hotspot name, hotspot password and dashboard password (leave blank to have
them generated), and prints them. It takes 2–3 minutes.
If you were connected over Wi-Fi, SSH drops at the end while the built-in Wi-Fi
turns into the hotspot. That's expected: your home Wi-Fi moves to the USB dongle.

### Sharing the Pi with other projects

PiShield keeps to its own folders (`/opt/pishield`, `/var/lib/pishield`), its own
Python environment and its own firewall table, so other projects' files and
packages aren't touched. What it does share:

| Shared thing | What happens |
|---|---|
| Built-in Wi-Fi | Becomes the hotspot. The Pi reaches your network through the USB dongle, usually with a **new IP address**: update anything that reaches the Pi by IP, or reserve the dongle's address in your router. |
| Port 80 | If another project uses it, the dashboard moves to a free port (e.g. `http://192.168.50.1:8080`) and the other project keeps port 80. |
| Port 53 (DNS) | Only one DNS server can run. Pi-hole / AdGuard Home must go first. |
| Docker | Docker blocks forwarded traffic by default; PiShield adds two rules to Docker's `DOCKER-USER` chain so hotspot devices reach the internet. |
| ufw / firewalld | The check tells you the one or two commands needed to let the hotspot through. |

## 3. Use it

1. Join the **PiShield** Wi-Fi on your phone or laptop.
2. Open **http://192.168.50.1** (or http://pishield.lan) and sign in.
3. **Network** → *Find networks* → join a Wi-Fi with internet. It's saved and
   rejoined automatically next time it's in range. Ethernet works too.
   Work and school networks that ask for a **username and password**
   (WPA2/WPA3-Enterprise, eduroam) are marked *Work / school login*; see below.
4. **Sites** → add the websites you want ads blocked on, or tap one of the
   *Recently visited* suggestions.

### Dashboard tabs

| Tab | What it does |
|---|---|
| Overview | Mode switch (Off / Selected sites / Everywhere), 24-hour chart, top blocked, Pi health |
| Sites | Websites to block ads on; per-site on/off; per-site extra domains; suggestions from what you browse |
| Activity | Live log of every lookup with one-tap **Allow** / **Block** |
| Filters | Ad lists (StevenBlack, AdGuard DNS, Peter Lowe, or add your own URL); Always-allow / Always-block lists |
| Network | Scan and join upstream Wi-Fi (password or work/school login), saved networks, hotspot name/password/band/channel |
| Devices | Who's connected, how many ads each device had blocked, which selected site they're on |
| Settings | Site window, forced DNS, upstream DNS servers, password, restart / reboot / shut down |

## How per-site blocking works

PiShield blocks ads at the DNS level: when a device asks "where is
`ads.doubleclick.net`?", PiShield answers "nowhere" and the ad never loads. DNS
doesn't say which page a request came from, so PiShield works it out per device:

- When a device looks up a site on your list (say `cnn.com` or `static.cnn.com`), that
  device is marked **on cnn.com** for 180 seconds (adjustable), refreshed every time it
  loads more from cnn.com.
- While a device is on a selected site, ad and tracker domains are blocked **for that
  device only**. Your other devices, and the same device on unlisted sites, aren't affected.
- Answers are given very short lifetimes (5–10 s) so devices keep checking back and
  the decision stays current as you move between sites.

Because it's per device and time-based, if you have cnn.com open and switch to
another tab within the window, that tab gets ad blocking too. Shorten the window
in Settings if that bothers you. **Everywhere** mode skips all of this and blocks
ads on every site for everyone.

### What it can't block

- **YouTube, Facebook, Instagram, Twitch, TikTok, Reddit, X, Hulu, Spotify** serve
  most of their ads from the same servers as their content. No DNS blocker (PiShield,
  Pi-hole, AdGuard Home) can remove those without breaking the site. Trackers and
  third-party ads on those sites are still blocked.
- Apps or browsers using their own encrypted DNS. PiShield redirects normal DNS,
  blocks DNS-over-TLS, and switches off Firefox's DNS-over-HTTPS and iCloud Private
  Relay on this network (Settings → *Make every device use PiShield*). A few apps
  hard-code DNS-over-HTTPS and will bypass it.
- If a site breaks, find the blocked domain in **Activity** and tap **Allow**.

## Work and school Wi-Fi (username + password)

Networks marked *Work / school login* use 802.1X. Tap **Join** and enter:

- **Username** and **password**: your normal work/school login (for eduroam,
  usually `you@your-school.edu`).
- **Certificate domain** (recommended): ask IT for the RADIUS server's domain,
  e.g. `radius.example.edu`. With it, the Pi checks the network's certificate
  before sending your password; without it, a fake network with the same name
  could capture your password.
- **Advanced**: PEAP + MSCHAPv2 works for most networks. Try TTLS + PAP if IT says so.

If the login is wrong, PiShield deletes the attempt instead of retrying in the
background, so repeated failures don't lock your account. Hidden networks: tap
*Hidden network…* and pick *Username + password*.

Not supported: networks that need a certificate file installed on the device
(EAP-TLS) or a device registration page. Also, **many workplaces don't allow
personal hotspots** and some detect and block them; check with IT first.

## Hotel and café Wi-Fi with a sign-in page

Join the network in the Network tab, then open any `http://` page on your phone.
The portal's sign-in page comes through PiShield; log in there and the whole Pi is
online. Keep *Upstream DNS* on **auto** for this to work.

## Maintenance

| Task | Command (on the Pi) |
|---|---|
| Logs | `journalctl -u pishield -f` |
| Restart | `sudo systemctl restart pishield` |
| Forgot dashboard password | `cd /opt/pishield && sudo PISHIELD_DATA=/var/lib/pishield venv/bin/python -m pishield passwd` |
| Update PiShield | copy the new folder over, run `sudo ./install.sh` again (settings are kept) |
| Remove | `sudo ./uninstall.sh` (add `--purge` to delete settings) |

Settings live in `/var/lib/pishield/config.json`; downloaded lists in `/var/lib/pishield/lists/`.

## Troubleshooting

- **No PiShield network appears.** `nmcli connection up pishield-hotspot` shows the
  error. A Wi-Fi country must be set (`sudo raspi-config` → Localisation → WLAN Country).
- **Hotspot works but no internet.** Network tab: is the dongle connected? Check
  `nmcli device` on the Pi. `lsusb` should list the dongle; some need a driver package.
- **Phone says "no internet" and won't open the dashboard.** Use the IP,
  `http://192.168.50.1`, not `pishield.lan`. Phones send DNS over mobile data when
  Wi-Fi has no internet.
- **5 GHz hotspot won't start.** The built-in radio only allows some channels in some
  countries. Try 36 or 149, or go back to 2.4 GHz.
- **Something else on port 53.** Pi-hole, AdGuard Home or another DNS server must be removed
  first. (Port 80 is fine: the dashboard moves to another port; the install summary shows it.)
- **Dashboard address.** If the installer moved the dashboard off port 80, it's
  `http://192.168.50.1:<port>`; check with `grep HTTP_PORT /etc/systemd/system/pishield.service`.
- **Enterprise Wi-Fi won't connect.** Check username/password and the certificate
  domain with IT, or try leaving the domain blank once to confirm the login works.
  `journalctl -u NetworkManager -n 50` shows the exact reason.

## Files

```
install.sh / uninstall.sh   set up / remove on the Pi
pishield/__main__.py        service entry point (+ passwd, init commands)
pishield/dnsserver.py       DNS server, blocking decisions, per-site logic, cache, stats
pishield/blocklists.py      list download + parsing (hosts, domains, adblock ||domain^)
pishield/network.py         nmcli control: upstream Wi-Fi, hotspot, leases, Pi health
pishield/firewall.py        nftables: force DNS, block DoT, keep dashboard/DNS hotspot-only
pishield/web.py             dashboard JSON API + login
pishield/static/            dashboard (plain HTML/CSS/JS, works on phones)
```

### Run it on a Mac/PC for development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

```bash
PISHIELD_DEV=1 PISHIELD_DATA=./devdata PISHIELD_DNS_PORT=5454 PISHIELD_HTTP_PORT=8091 PISHIELD_BIND=127.0.0.1 .venv/bin/python -m pishield
```

Then `dig @127.0.0.1 -p 5454 doubleclick.net` and open http://127.0.0.1:8091
(password `pishield`). Network features need a Pi.
