"""Telegram bot handlers."""

import asyncio
import logging
from functools import wraps

from aiogram import Router, types, F
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile

from .config import config
from .device import Device, DeviceError
from .storage import PlaceStore

log = logging.getLogger(__name__)

router = Router()
store = PlaceStore(config.db_path)
device = Device(config.device_serial, config.allsports_pkg)


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
        "Commands:\n"
        "/places — saved venues\n"
        "/addplace name lat lon — save a venue\n"
        "/delplace name — remove a venue\n"
        "/checkin name — spoof location & request check-in\n"
        "/stop — stop location spoof\n"
        "/shot — screenshot the duty phone\n"
        "/dump — UI hierarchy for selector calibration\n"
        "/status — device connectivity"
    )


@router.message(Command("places"))
@owner_only
async def cmd_places(m: types.Message):
    places = store.list()
    if not places:
        return await m.answer("No places yet. /addplace name lat lon")
    await m.answer("\n".join(f"• {p.name}: {p.lat}, {p.lon}" for p in places))


@router.message(Command("addplace"))
@owner_only
async def cmd_addplace(m: types.Message, command: CommandObject):
    try:
        name, coordinates = command.args.split(maxsplit=1)
        lat, lon = coordinates.replace(",", " ").split()
        latitude, longitude = float(lat), float(lon)
        store.add(name, latitude, longitude)
    except (ValueError, AttributeError):
        return await m.answer(
            "Usage: /addplace name lat lon\n"
            "Yandex Maps format is supported: /addplace dc 53.915323, 27.571478"
        )
    await m.answer(f"Saved {name} ({latitude}, {longitude})")


@router.message(Command("delplace"))
@owner_only
async def cmd_delplace(m: types.Message, command: CommandObject):
    if not command.args or not store.remove(command.args.strip()):
        return await m.answer("Not found. /places to list")
    await m.answer("Removed")


@router.message(Command("checkin"))
@owner_only
async def cmd_checkin(m: types.Message, command: CommandObject):
    if not command.args:
        return await m.answer("Usage: /checkin name")
    place = store.get(command.args.strip())
    if not place:
        return await m.answer("Unknown place. /places to list")

    progress = await m.answer(f"Connecting to device at {config.device_serial}…")
    try:
        await run_in_thread(device.connect)
        await progress.edit_text(f"Spoofing to {place.name} ({place.lat}, {place.lon})…")
        steps = await run_in_thread(
            device.checkin, place.name, place.lat, place.lon
        )
        await progress.edit_text("Request sent — waiting for admin confirmation.")
        for s in steps:
            if s.screenshot:
                await m.answer_photo(
                    BufferedInputFile(s.screenshot, filename=f"{s.name}.png"),
                    caption=s.name,
                )
    except Exception as e:
        log.exception("checkin failed")
        await progress.edit_text(f"Failed: {e}")
        try:
            shot = await run_in_thread(device.screenshot)
            await m.answer_photo(BufferedInputFile(shot, filename="screen.png"))
        except Exception:
            pass


@router.message(Command("stop"))
@owner_only
async def cmd_stop(m: types.Message):
    try:
        await run_in_thread(device.connect)
        await run_in_thread(device.clear_location)
        await m.answer("Spoof stopped")
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
        await m.answer(f"Device OK\nForeground: {app}")
    except DeviceError as e:
        await m.answer(f"Device unreachable: {e}")
