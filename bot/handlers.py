"""Telegram bot handlers."""

import asyncio
import logging
from functools import wraps

from aiogram import Router, types, F
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from .config import config
from .device import Device, DeviceError
from .storage import PlaceStore

log = logging.getLogger(__name__)

router = Router()
store = PlaceStore(config.db_path)
device = Device(config.device_serial, config.allsports_pkg)

BTN_CHECKIN = "Запросить визит"
BTN_PLACES = "Объекты"
BTN_ADD_PLACE = "Добавить объект"
BTN_SHOT = "Скриншот"
BTN_STATUS = "Статус"
BTN_STOP = "Остановить геопозицию"
BTN_DUMP = "Дамп UI"


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_CHECKIN)],
            [
                KeyboardButton(text=BTN_PLACES),
                KeyboardButton(text=BTN_ADD_PLACE),
            ],
            [
                KeyboardButton(text=BTN_SHOT),
                KeyboardButton(text=BTN_STATUS),
            ],
            [KeyboardButton(text=BTN_STOP)],
            [KeyboardButton(text=BTN_DUMP)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def place_menu() -> InlineKeyboardMarkup | None:
    places = store.list()
    if not places:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=p.name, callback_data=f"checkin:{p.name}")]
            for p in places
        ]
    )


def owner_only(handler):
    @wraps(handler)
    async def wrapper(m: types.Message, *args, **kwargs):
        if m.from_user.id not in config.allowed_users:
            return await m.answer("Access denied.")
        return await handler(m, *args, **kwargs)
    return wrapper


def run_in_thread(fn, *a, **kw):
    loop = asyncio.get_running_loop()
    return loop.run_in_executor(None, lambda: fn(*a, **kw))


@router.message(Command("start", "help"))
@owner_only
async def cmd_help(m: types.Message):
    await m.answer(
        "Выбери действие кнопкой ниже.\n\n"
        "Commands:\n"
        "/places — saved venues\n"
        "/addplace name lat lon — save a venue\n"
        "/delplace name — remove a venue\n"
        "/checkin name — spoof location & request check-in\n"
        "/stop — stop location spoof\n"
        "/shot — screenshot the duty phone\n"
        "/dump — UI hierarchy for selector calibration\n"
        "/status — device connectivity",
        reply_markup=main_menu(),
    )


@router.message(Command("places"))
@owner_only
async def cmd_places(m: types.Message):
    places = store.list()
    if not places:
        return await m.answer("No places yet. /addplace name lat lon", reply_markup=main_menu())
    await m.answer("\n".join(f"• {p.name}: {p.lat}, {p.lon}" for p in places), reply_markup=main_menu())


@router.message(Command("addplace"))
@owner_only
async def cmd_addplace(m: types.Message, command: CommandObject):
    try:
        parts = command.args.replace(",", " ").split()
        name = " ".join(parts[:-2])
        latitude, longitude = float(parts[-2]), float(parts[-1])
        if not name:
            raise ValueError
        store.add(name, latitude, longitude)
    except (ValueError, AttributeError, IndexError):
        return await m.answer(
            "Usage: /addplace name lat lon (имя можно с пробелами)\n"
            "Yandex Maps format is supported: /addplace Dance Cafe 53.915323, 27.571478"
        )
    await m.answer(f"Saved {name} ({latitude}, {longitude})", reply_markup=main_menu())


@router.message(Command("delplace"))
@owner_only
async def cmd_delplace(m: types.Message, command: CommandObject):
    if not command.args or not store.remove(command.args.strip()):
        return await m.answer("Not found. /places to list", reply_markup=main_menu())
    await m.answer("Removed", reply_markup=main_menu())


@router.message(Command("checkin"))
@owner_only
async def cmd_checkin(m: types.Message, command: CommandObject):
    if not command.args:
        return await m.answer("Usage: /checkin name", reply_markup=main_menu())
    place = store.get(command.args.strip())
    if not place:
        return await m.answer("Unknown place. /places to list", reply_markup=main_menu())

    await perform_checkin(m, place)


async def perform_checkin(m: types.Message, place):
    progress = await m.answer(f"Connecting to device at {config.device_serial}…")
    loop = asyncio.get_running_loop()
    last_step = ["connect"]

    async def _set_progress(text: str) -> None:
        try:
            await progress.edit_text(text)
        except Exception:
            pass

    def on_step(name: str) -> None:
        last_step[0] = name
        asyncio.run_coroutine_threadsafe(
            _set_progress(f"{place.name}: {name}…"), loop
        )

    try:
        await run_in_thread(device.connect)
        await progress.edit_text(f"Spoofing to {place.name} ({place.lat}, {place.lon})…")
        steps = await run_in_thread(
            device.checkin, place.name, place.lat, place.lon, on_step=on_step
        )
        await progress.edit_text("Request sent — waiting for admin confirmation.", reply_markup=main_menu())
        for s in steps:
            if s.screenshot:
                await m.answer_photo(
                    BufferedInputFile(s.screenshot, filename=f"{s.name}.png"),
                    caption=s.name,
                )
    except Exception as e:
        log.exception("checkin failed")
        await progress.edit_text(f"Failed at {last_step[0]}: {e}", reply_markup=main_menu())
        try:
            shot = await run_in_thread(device.screenshot)
            await m.answer_photo(BufferedInputFile(shot, filename="screen.png"))
        except Exception:
            pass


@router.message(F.text == BTN_CHECKIN)
@owner_only
async def menu_checkin(m: types.Message):
    keyboard = place_menu()
    if not keyboard:
        return await m.answer(
            "Объектов пока нет. Нажми «Добавить объект».",
            reply_markup=main_menu(),
        )
    await m.answer("Выбери объект:", reply_markup=keyboard)


@router.callback_query(F.data.startswith("checkin:"))
async def callback_checkin(c: types.CallbackQuery):
    if c.from_user.id not in config.allowed_users:
        return await c.answer("Access denied.", show_alert=True)
    place = store.get(c.data.removeprefix("checkin:"))
    if not place:
        return await c.answer("Объект не найден.", show_alert=True)
    await c.answer()
    await perform_checkin(c.message, place)


@router.message(F.text == BTN_PLACES)
@owner_only
async def menu_places(m: types.Message):
    await cmd_places(m)


@router.message(F.text == BTN_ADD_PLACE)
@owner_only
async def menu_add_place(m: types.Message):
    await m.answer(
        "Пришли объект одной строкой:\n"
        "/addplace имя 53.915323, 27.571478\n\n"
        "Имя — как в списке Allsports, можно с пробелами.\n"
        "Координаты можно вставить прямо из Яндекс Карт.",
        reply_markup=main_menu(),
    )


@router.message(Command("stop"))
@owner_only
async def cmd_stop(m: types.Message):
    try:
        await run_in_thread(device.connect)
        await run_in_thread(device.clear_location)
        await m.answer("Spoof stopped", reply_markup=main_menu())
    except DeviceError as e:
        await m.answer(f"Failed: {e}")


@router.message(Command("shot"))
@owner_only
async def cmd_shot(m: types.Message):
    try:
        await run_in_thread(device.connect)
        png = await run_in_thread(device.screenshot)
        await m.answer_photo(BufferedInputFile(png, filename="screen.png"))
    except DeviceError as e:
        await m.answer(f"Failed: {e}")


@router.message(Command("dump"))
@owner_only
async def cmd_dump(m: types.Message):
    try:
        await run_in_thread(device.connect)
        xml = await run_in_thread(device.dump_ui)
        await m.answer_document(
            BufferedInputFile(xml.encode(), filename="hierarchy.xml"),
            caption="UI hierarchy",
        )
    except Exception as e:
        log.exception("dump failed")
        await m.answer(f"Failed: {e}")


@router.message(Command("status"))
@owner_only
async def cmd_status(m: types.Message):
    try:
        await run_in_thread(device.connect)
        app = await run_in_thread(device.current_app)
        await m.answer(f"Device OK\nForeground: {app}", reply_markup=main_menu())
    except DeviceError as e:
        await m.answer(f"Device unreachable: {e}", reply_markup=main_menu())


@router.message(F.text == BTN_SHOT)
@owner_only
async def menu_shot(m: types.Message):
    await cmd_shot(m)


@router.message(F.text == BTN_STATUS)
@owner_only
async def menu_status(m: types.Message):
    await cmd_status(m)


@router.message(F.text == BTN_STOP)
@owner_only
async def menu_stop(m: types.Message):
    await cmd_stop(m)


@router.message(F.text == BTN_DUMP)
@owner_only
async def menu_dump(m: types.Message):
    await cmd_dump(m)
