# Deploy on the Proxmox host

Everything runs in one Debian LXC on the Proxmox box. Telegram reaches the bot
through a Cloudflare Tunnel (free, no white IP needed).

## 1. LXC container

Debian 12 template, unprivileged is fine. Inside:

```bash
apt update && apt install -y python3-venv android-tools-adb curl
python3 -m venv /opt/asbot && /opt/asbot/bin/pip install -r requirements.txt
git clone <repo> /opt/ra-razorenie-as-pro-tg-bot
cd /opt/ra-razorenie-as-pro-tg-bot && /opt/asbot/bin/pip install -r requirements.txt
```

## 2. Env file `/opt/ra-razorenie-as-pro-tg-bot/.env`

```
BOT_TOKEN=123:abc           # from @BotFather
ALLOWED_USER_IDS=123456789  # your Telegram user id (@userinfobot)
WEBHOOK_BASE_URL=https://asbot.example.com
DEVICE_SERIAL=192.168.1.50:5555   # duty phone LAN ip
WEBHOOK_PORT=8080
DB_PATH=/var/lib/asbot/places.db
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

## 4. Cloudflare Tunnel

```bash
cloudflared tunnel login
cloudflared tunnel create asbot
cloudflared tunnel route dns asbot asbot.example.com
# config: http://localhost:8080 -> /webhook is the only public path
cloudflared service install
```

Point `WEBHOOK_BASE_URL` at the tunnel hostname. The bot registers the
webhook itself on startup (`setWebhook`).

## 5. Phone

See `device_setup.md`. Phone gets a DHCP reservation on the Keenetic/Mikrotik,
`adb tcpip 5555` once over USB, then `scripts/setup_device.sh <ip>`.

After a phone reboot: plug USB once, `adb tcpip 5555`, unplug. (Android < 11 —
i.e. the Redmi Note 7 — has no wireless debugging pairing; the Samsung A26
keeps wireless debugging but the TCP listener still dies on reboot.)
