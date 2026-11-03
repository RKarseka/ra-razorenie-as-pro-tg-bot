"""Webhook entry point (Telegram 'serverless' style: no polling)."""

import logging

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from .config import config
from .handlers import router, main_menu

logging.basicConfig(level=logging.INFO)

BOT_COMMANDS = [
    BotCommand(command="checkin", description="Запросить визит"),
    BotCommand(command="places", description="Объекты"),
    BotCommand(command="addplace", description="Добавить объект"),
    BotCommand(command="delplace", description="Удалить объект"),
    BotCommand(command="shot", description="Скриншот телефона"),
    BotCommand(command="status", description="Статус устройства"),
    BotCommand(command="stop", description="Остановить геопозицию"),
    BotCommand(command="dump", description="Дамп UI"),
    BotCommand(command="update", description="Обновить код и перезапустить бота"),
    BotCommand(command="start", description="Меню"),
]


async def set_commands(bot: Bot):
    await bot.set_my_commands(BOT_COMMANDS)
    logging.info("bot command menu set")


async def on_startup(bot: Bot):
    url = config.webhook_base_url.rstrip("/") + config.webhook_path
    await bot.set_webhook(url, allowed_updates=["message"])
    logging.info("webhook set to %s", url)


async def greet_users(bot: Bot):
    """Restore the reply keyboard after every (re)start."""
    for uid in config.allowed_users:
        try:
            await bot.send_message(
                uid, "Бот запущен и готов к работе.",
                reply_markup=main_menu(),
            )
        except Exception:
            logging.exception("greeting to %s failed", uid)


def main():
    bot = Bot(token=config.bot_token)
    dp = Dispatcher()
    dp.include_router(router)
    dp.startup.register(set_commands)
    dp.startup.register(greet_users)

    if config.mode != "webhook":
        import asyncio

        asyncio.run(dp.start_polling(bot))
        return

    if not config.webhook_base_url:
        raise RuntimeError("MODE=webhook requires WEBHOOK_BASE_URL")
    dp.startup.register(on_startup)

    app = web.Application()
    SimpleRequestHandler(dp, bot).register(app, path=config.webhook_path)
    setup_application(app, dp, bot=bot)
    web.run_app(app, host=config.webhook_host, port=config.webhook_port)


if __name__ == "__main__":
    main()
