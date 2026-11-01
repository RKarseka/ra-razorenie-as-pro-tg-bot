"""Webhook entry point (Telegram 'serverless' style: no polling)."""

import logging

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from .config import config
from .handlers import router

logging.basicConfig(level=logging.INFO)


async def on_startup(bot: Bot):
    url = config.webhook_base_url.rstrip("/") + config.webhook_path
    await bot.set_webhook(url, allowed_updates=["message"])
    logging.info("webhook set to %s", url)


def main():
    bot = Bot(token=config.bot_token)
    dp = Dispatcher()
    dp.include_router(router)
    dp.startup.register(on_startup)

    app = web.Application()
    SimpleRequestHandler(dp, bot).register(app, path=config.webhook_path)
    setup_application(app, dp, bot=bot)
    web.run_app(app, host=config.webhook_host, port=config.webhook_port)


if __name__ == "__main__":
    main()
