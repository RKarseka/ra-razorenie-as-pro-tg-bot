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

# Pending venue picks per user: names discovered by /addplace.
_pending_venues: dict[int, dict] = {}
# Place names shown by /places, so delete callbacks fit 64-byte callback_data.
_pending_del: dict[int, list[str]] = {}
# Venue whose services were offered for the visit, per user.
_pending_svc: dict[int, object] = {}
# Service multi-select at venue registration, per user.
_pending_svc_pick: dict[int, dict] = {}
# Users asked to type a venue name (from «Добавить объект»).
_awaiting_place_name: set[int] = set()
# Geocoding candidates offered to each user.
_pending_geo: dict[int, list] = {}

BTN_CHECKIN = "Запросить визит"
BTN_PLACES = "Объекты"
BTN_ADD_PLACE = "Добавить объект"
BTN_SHOT = "Скриншот"
BTN_STATUS = "Статус"
BTN_STOP = "Остановить геопозицию"
BTN_DUMP = "Дамп UI"
BTN_UPDATE = "Обновить бота"


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
            [KeyboardButton(text=BTN_DUMP), KeyboardButton(text=BTN_UPDATE)],
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


def norm_name(s: str) -> str:
    """The normalized form checkin matches against venue rows (same as device)."""
    import re as _re
    return _re.sub(r"\s+", " ", _re.sub(r"[^\w\s]", " ", s).lower().replace("ё", "е")).strip()


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
    lines = [
        f"• {p.name}: {p.lat}, {p.lon}" + (f" — {p.address}" if p.address else "")
        for p in places
    ]
    _pending_del[m.from_user.id] = [p.name for p in places]
    buttons = [
        [InlineKeyboardButton(text=f"🗑 {p.name}", callback_data=f"del:{i}")]
        for i, p in enumerate(places)
    ]
    await m.answer(
        "\n".join(lines) + "\n\n🗑 — удалить объект",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


def geocode(query: str, limit: int = 5) -> list[tuple[float, float, str]]:
    """Resolve a place name to candidate (lat, lon, display_name) tuples
    via OSM Nominatim, biased to Belarus."""
    import json as _json
    import urllib.parse, urllib.request

    q = urllib.parse.urlencode({
        "q": query,
        "format": "json",
        "countrycodes": "by",
        "limit": str(limit),
    })
    req = urllib.request.Request(
        f"https://nominatim.openstreetmap.org/search?{q}",
        headers={"User-Agent": "as-checkin-bot/1.0"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        data = _json.load(r)
    return [(float(d["lat"]), float(d["lon"]), d["display_name"]) for d in data]


@router.message(Command("addplace"))
@owner_only
async def cmd_addplace(m: types.Message, command: CommandObject):
    name = ""
    try:
        parts = command.args.replace(",", " ").split()
        latitude, longitude = float(parts[-2]), float(parts[-1])
        name = " ".join(parts[:-2])
    except (ValueError, AttributeError, IndexError):
        query = (command.args or "").strip()
        if not query:
            return await m.answer(
                "Usage: /addplace [name] lat lon — или просто название:\n"
                "/addplace Dance Cafe — найду координаты сам"
            )
        try:
            found = await run_in_thread(geocode, query, 1)
        except Exception as e:
            log.exception("geocode failed")
            return await m.answer(f"Геокодер недоступен: {e}", reply_markup=main_menu())
        if not found:
            return await m.answer(
                f"Не нашёл «{query}» на карте. Пришли координаты из Яндекс Карт.",
                reply_markup=main_menu(),
            )
        latitude, longitude, disp = found[0]
        await m.answer(
            f"Нашёл: {disp}\nКоординаты: {latitude}, {longitude}",
            reply_markup=main_menu(),
        )
    await discover_venues(m, m.from_user.id, name, latitude, longitude)


async def discover_venues(m: types.Message, uid: int, name: str,
                          latitude: float, longitude: float) -> None:
    """Save the typed name if any, set the point and offer nearby venues."""
    if name:
        store.add(name, latitude, longitude)
        await m.answer(f"Saved {name} ({latitude}, {longitude})", reply_markup=main_menu())

    progress = await m.answer("Смотрю, какие заведения рядом с этой точкой…")
    try:
        await run_in_thread(device.connect)
        names, shot = await run_in_thread(device.venues_near, latitude, longitude)
        if names:
            _pending_venues[uid] = {
                "names": names,
                "lat": latitude,
                "lon": longitude,
                "saved_as": name or None,
            }
            listing = "\n".join(f"• {t} — {d}" for t, d in names)
            buttons = [
                [InlineKeyboardButton(text=t, callback_data=f"save:{i}")]
                for i, (t, _d) in enumerate(names)
            ]
            await progress.edit_text(
                "Заведения рядом с этой точкой:\n" + listing
                + "\n\nНажми на нужное — сохраню объект под этим названием.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            )
        else:
            await progress.edit_text("Список заведений не распарсился — вот скриншот.")
        await m.answer_photo(
            BufferedInputFile(shot, filename="venues.png"),
            caption="Вход по локации",
        )
    except Exception as e:
        log.exception("venues_near failed")
        await progress.edit_text(f"Не удалось открыть список: {e}")


@router.callback_query(F.data.startswith("del:"))
async def callback_del_place(c: types.CallbackQuery):
    if c.from_user.id not in config.allowed_users:
        return await c.answer("Access denied.", show_alert=True)
    try:
        name = _pending_del[c.from_user.id][int(c.data.removeprefix("del:"))]
    except (KeyError, IndexError, ValueError):
        return await c.answer("Список устарел — открой «Объекты» заново.", show_alert=True)
    if store.remove(name):
        await c.answer(f"Удалено: {name}")
        await c.message.answer(f"Removed {name}", reply_markup=main_menu())
    else:
        await c.answer("Уже удалён.", show_alert=True)


@router.callback_query(F.data.startswith("save:"))
async def callback_save_place(c: types.CallbackQuery):
    if c.from_user.id not in config.allowed_users:
        return await c.answer("Access denied.", show_alert=True)
    pend = _pending_venues.get(c.from_user.id)
    try:
        name, detail = pend["names"][int(c.data.removeprefix("save:"))]
        lat, lon = pend["lat"], pend["lon"]
    except (TypeError, KeyError, IndexError, ValueError):
        return await c.answer(
            "Список устарел — отправь /addplace заново.", show_alert=True
        )
    store.add(name, lat, lon, address=detail)
    if pend.get("saved_as") and pend["saved_as"] != name:
        store.remove(pend["saved_as"])
    pend["saved_as"] = name
    await c.answer(f"Сохранено: {name}")
    await c.message.answer(
        f"Сохранено: {name}\n"
        f"Адрес: {detail}\n"
        f"Координаты: {lat}, {lon}\n"
        f"Ищется в списке как: «{norm_name(name)}»",
        reply_markup=main_menu(),
    )

    prog = await c.message.answer("Собираю услуги заведения…")
    try:
        await run_in_thread(device.connect)
        services = await run_in_thread(device.services_of_venue, name, lat, lon)
    except Exception as e:
        log.exception("services_of_venue failed")
        return await prog.edit_text(f"Не удалось собрать услуги: {e}")
    if not services:
        return await prog.edit_text("Услуги не распарсились.")

    _pending_svc_pick[c.from_user.id] = {
        "place": (name, lat, lon, detail),
        "services": services,
        "chosen": set(),
    }
    await _render_service_pick(prog, name, services, set())


async def _render_service_pick(prog, venue: str, services: list[str], chosen: set):
    buttons = [
        [InlineKeyboardButton(
            text=("✅ " if i in chosen else "") + s,
            callback_data=f"svcpick:{i}",
        )]
        for i, s in enumerate(services)
    ]
    buttons.append([InlineKeyboardButton(text="Готово", callback_data="svcdone")])
    await prog.edit_text(
        f"Услуги «{venue}» — отметь нужные и жми «Готово»:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.callback_query(F.data.startswith("svcpick:"))
async def callback_svc_pick(c: types.CallbackQuery):
    if c.from_user.id not in config.allowed_users:
        return await c.answer("Access denied.", show_alert=True)
    pend = _pending_svc_pick.get(c.from_user.id)
    try:
        i = int(c.data.removeprefix("svcpick:"))
        pend["services"][i]
    except (TypeError, KeyError, IndexError, ValueError):
        return await c.answer("Список устарел.", show_alert=True)
    pend["chosen"].symmetric_difference_update({i})
    venue = pend["place"][0]
    await _render_service_pick(c.message, venue, pend["services"], pend["chosen"])
    await c.answer()


@router.callback_query(F.data == "svcdone")
async def callback_svc_done(c: types.CallbackQuery):
    if c.from_user.id not in config.allowed_users:
        return await c.answer("Access denied.", show_alert=True)
    pend = _pending_svc_pick.pop(c.from_user.id, None)
    if not pend:
        return await c.answer("Список устарел.", show_alert=True)
    name, lat, lon, detail = pend["place"]
    chosen = [s for i, s in enumerate(pend["services"]) if i in pend["chosen"]]
    store.add(name, lat, lon, address=detail, services=chosen)
    listing = "\n".join(f"• {s}" for s in chosen) or "— не выбраны"
    await c.message.edit_text(f"Услуги «{name}» сохранены:\n" + listing)
    await c.answer("Сохранено")


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

    await offer_services(m, m.from_user.id, place)


async def offer_services(m: types.Message, uid: int, place) -> None:
    """Pick a saved service first when the venue has any, else check in."""
    if place.services:
        _pending_svc[uid] = place
        buttons = [
            [InlineKeyboardButton(text=s, callback_data=f"svc:{i}")]
            for i, s in enumerate(place.services)
        ]
        await m.answer(
            f"{place.name}: выбери услугу",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )
    else:
        await perform_checkin(m, place)


@router.callback_query(F.data.startswith("svc:"))
async def callback_service(c: types.CallbackQuery):
    if c.from_user.id not in config.allowed_users:
        return await c.answer("Access denied.", show_alert=True)
    place = _pending_svc.get(c.from_user.id)
    try:
        service = place.services[int(c.data.removeprefix("svc:"))]
    except (AttributeError, IndexError, ValueError):
        return await c.answer(
            "Список устарел — выбери объект заново.", show_alert=True
        )
    await c.answer(service)
    await perform_checkin(c.message, place, service)


async def perform_checkin(m: types.Message, place, service: str | None = None):
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
            device.checkin, place.name, place.lat, place.lon,
            on_step=on_step, service=service,
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
        await progress.edit_text(f"Failed at {last_step[0]}: {e}")
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
    await offer_services(c.message, c.from_user.id, place)


@router.message(F.text == BTN_PLACES)
@owner_only
async def menu_places(m: types.Message):
    await cmd_places(m)


@router.message(F.text == BTN_ADD_PLACE)
@owner_only
async def menu_add_place(m: types.Message):
    _awaiting_place_name.add(m.from_user.id)
    await m.answer(
        "Напиши название заведения — найду его на карте и предложу варианты.\n"
        "(или координаты из Яндекс Карт: 53.915323, 27.571478)",
        reply_markup=main_menu(),
    )


@router.message(Command("update"))
@router.message(F.text == BTN_UPDATE)
@owner_only
async def cmd_update(m: types.Message):
    """Self-update: git pull in the repo dir + restart the systemd unit."""
    import os

    repo_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", repo_dir, "pull", "--ff-only",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    text = out.decode(errors="replace").strip()[:3500]
    await m.answer(f"```\n{text}\n```", parse_mode="Markdown")
    await m.answer("Перезапускаю бота… (если он молчит пару минут — проверь journalctl)")
    await asyncio.create_subprocess_exec("systemctl", "restart", "asbot")


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


@router.callback_query(F.data.startswith("geo:"))
async def callback_geo_pick(c: types.CallbackQuery):
    if c.from_user.id not in config.allowed_users:
        return await c.answer("Access denied.", show_alert=True)
    try:
        lat, lon, disp = _pending_geo[c.from_user.id][int(c.data.removeprefix("geo:"))]
    except (KeyError, IndexError, ValueError):
        return await c.answer("Список устарел — нажми «Добавить объект» заново.",
                              show_alert=True)
    await c.answer(disp.split(",")[0])
    await c.message.edit_text(f"Выбрано: {disp}\nКоординаты: {lat}, {lon}")
    await discover_venues(c.message, c.from_user.id, "", lat, lon)


@router.message(F.text)
async def free_text(m: types.Message):
    uid = m.from_user.id
    if uid not in _awaiting_place_name or m.from_user.id not in config.allowed_users:
        return
    _awaiting_place_name.discard(uid)
    query = m.text.strip()
    parts = query.replace(",", " ").split()
    try:
        lat, lon = float(parts[-2]), float(parts[-1])
        return await discover_venues(m, uid, "", lat, lon)
    except (ValueError, IndexError):
        pass
    wait = await m.answer(f"Ищу «{query}» на карте…")
    try:
        found = await run_in_thread(geocode, query)
    except Exception as e:
        log.exception("geocode failed")
        return await wait.edit_text(f"Геокодер недоступен: {e}")
    if not found:
        return await wait.edit_text(
            f"Не нашёл «{query}» на карте. Пришли координаты из Яндекс Карт."
        )
    if len(found) == 1:
        lat, lon, disp = found[0]
        await wait.edit_text(f"Нашёл: {disp}\nКоординаты: {lat}, {lon}")
        return await discover_venues(m, uid, "", lat, lon)
    _pending_geo[uid] = found
    buttons = [
        [InlineKeyboardButton(text=disp[:64], callback_data=f"geo:{i}")]
        for i, (_lat, _lon, disp) in enumerate(found)
    ]
    await wait.edit_text(
        "Несколько вариантов — выбери нужный:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
