"""Bot configuration — all values come from environment variables."""

import os
from dataclasses import dataclass, field


@dataclass
class Config:
    bot_token: str = field(default_factory=lambda: os.environ["BOT_TOKEN"])
    # Telegram user IDs allowed to use the bot (comma-separated). First entry = owner.
    allowed_users: set[int] = field(
        default_factory=lambda: {
            int(x) for x in os.environ["ALLOWED_USER_IDS"].split(",") if x.strip()
        }
    )
    # Public HTTPS base URL Telegram calls (from Cloudflare Tunnel or direct).
    webhook_base_url: str = field(default_factory=lambda: os.environ["WEBHOOK_BASE_URL"])
    webhook_path: str = "/webhook"
    webhook_host: str = os.environ.get("WEBHOOK_HOST", "0.0.0.0")
    webhook_port: int = int(os.environ.get("WEBHOOK_PORT", "8080"))

    # ADB endpoint of the duty phone, e.g. "192.168.1.50:5555"
    device_serial: str = field(
        default_factory=lambda: os.environ.get("DEVICE_SERIAL", "192.168.1.50:5555")
    )

    allsports_pkg: str = "by.allsports.holder"
    fakegps_pkg: str = "com.lexa.fakegps"

    db_path: str = os.environ.get("DB_PATH", "places.db")


config = Config()
