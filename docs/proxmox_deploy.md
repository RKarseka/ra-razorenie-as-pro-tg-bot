# Deploy on the Proxmox host

Everything runs in one Debian LXC on the Proxmox box.

Two modes:

- **MODE=polling** (default): bot polls Telegram itself — nothing is exposed,
  no domain/tunnel/cert needed. Recommended.
- **MODE=webhook**: needs a public HTTPS URL — Cloudflare Tunnel (domain
  required) or Tailscale Funnel (no domain needed, stable `*.ts.net` name).

## 1. LXC container

Debian 12 template, unprivileged is fine. Inside:

```bash
apt update && apt install -y python3-venv android-tools-adb git curl
git clone <repo> /opt/ra-razorenie-as-pro-tg-bot
python3 -m venv /opt/asbot
/opt/asbot/bin/pip install -r /opt/ra-razorenie-as-pro-tg-bot/requirements.txt
mkdir -p /var/lib/asbot
```

## 2. Env file `/opt/ra-razorenie-as-pro-tg-bot/.env`

```
BOT_TOKEN=123:abc           # from @BotFather
ALLOWED_USER_IDS=360053442
DEVICE_SERIAL=192.168.1.50:5555   # duty phone LAN ip
DB_PATH=/var/lib/asbot/places.db
MODE=polling
```

## 3. systemd unit `/etc/systemd/system/asbot.service`

```
[Unit]
Description=Allsports check-in bot
After=network-online.target

[Service]
EnvironmentFile=/opt/ra-razorenie-as-pro-tg-bot/.env
WorkingDirectory=/opt/ra-razorenie-as-pro-tg-bot
ExecStart=/opt/asbot/bin/python -m bot.main
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

`systemctl enable --now asbot`

## 4. Public endpoint — only for MODE=webhook

Skip entirely with MODE=polling.

Option A — Tailscale Funnel (no domain needed):

```bash
tailscale up                       # auth once
tailscale funnel --bg 8080         # -> https://<lxc>.<tailnet>.ts.net
# enable HTTPS certificates + MagicDNS in the tailnet admin panel once
```

Option B — Cloudflare Tunnel (needs a domain on Cloudflare):

```bash
cloudflared tunnel login
cloudflared tunnel create asbot
cloudflared tunnel route dns asbot asbot.example.com
cloudflared service install
```

Point `WEBHOOK_BASE_URL` at the resulting hostname. The bot registers the
webhook itself on startup (`setWebhook`).

## 5. Phone

See `device_setup.md`. Phone gets a DHCP reservation on the Keenetic/Mikrotik,
`adb tcpip 5555` once over USB, then `scripts/setup_device.sh <ip>`.

After a phone reboot: plug USB once, `adb tcpip 5555`, unplug. (Android < 11 —
i.e. the Redmi Note 7 — has no wireless debugging pairing; the Samsung A26
keeps wireless debugging but the TCP listener still dies on reboot.)
