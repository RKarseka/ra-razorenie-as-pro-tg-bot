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
device = Device(config.device_serial, config.allsports_pkg, config.device_pin)

# Pending venue picks per user: names discovered by /addplace.
_pending_venues: dict[int, dict] = {}
# Place names shown by /places, so delete callbacks fit 64-byte callback_data.
# Last «Объекты» listing per user (Place rows, for nested actions).
_pending_places: dict[int, list] = {}
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
BTN_STOP = "Остановить визит"
BTN_DUMP = "Дамп UI"
BTN_UPDATE = "Обновить бота"
BTN_SETTINGS = "Настройки"


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=BTN_CHECKIN),
                KeyboardButton(text=BTN_PLACES),
                KeyboardButton(text=BTN_ADD_PLACE),
            ],
            [
                KeyboardButton(text=BTN_STOP),
                KeyboardButton(text=BTN_SHOT),
                KeyboardButton(text=BTN_STATUS),
            ],
            [KeyboardButton(text=BTN_DUMP), KeyboardButton(text=BTN_UPDATE),
             KeyboardButton(text=BTN_SETTINGS)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def place_menu(uid: int) -> InlineKeyboardMarkup | None:
    places = store.list()
    if not places:
        return None
    _pending_places[uid] = places
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=p.name, callback_data=f"checkin:{i}")]
            for i, p in enumerate(places)
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
            return  # silently ignore — strangers see a dead bot
        return await handler(m, *args, **kwargs)
    return wrapper


def run_in_thread(fn, *a, **kw):
    loop = asyncio.get_running_loop()
    return loop.run_in_executor(None, lambda: fn(*a, **kw))


async def _drop(msg) -> None:
    """Delete a transient bot message, ignoring failures."""
    try:
        await msg.delete()
    except Exception:
        pass


def _track(uid: int, kind: str, *ids: int) -> None:
    """Persist transient message ids so they can be deleted later,
    including after a restart."""
    store.set_setting(f"trk:{uid}:{kind}", ",".join(str(i) for i in ids if i))


async def _untrack(bot, uid: int, kind: str) -> None:
    """Delete the stored transient messages of a kind and clear the list."""
    raw = store.get_setting(f"trk:{uid}:{kind}", "")
    if not raw:
        return
    for s in raw.split(","):
        try:
            await bot.delete_message(uid, int(s))
        except Exception:
            pass
    store.set_setting(f"trk:{uid}:{kind}", "")


@router.message(Command("start", "help"))
@owner_only
async def cmd_help(m: types.Message):
    await _drop(m)
    await _untrack(m.bot, m.from_user.id, "menu")
    r = await m.answer("Выбери действие кнопкой ниже.",
                       reply_markup=main_menu())
    _track(m.from_user.id, "menu", r.message_id)


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
    _pending_places[m.from_user.id] = places
    buttons = [
        [InlineKeyboardButton(text=p.name, callback_data=f"plc:{i}")]
        for i, p in enumerate(places)
    ]
    await m.answer(
        "\n".join(lines) + "\n\nНажми на объект — откроются действия.",
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


def coords_from_link(text: str) -> tuple[float, float] | None:
    """Pull (lat, lon) out of a Yandex/Google maps share link, resolving
    short URLs (yandex.ru/maps/-/… → full URL with ll=)."""
    import re as _re
    import urllib.request

    m = _re.search(r"https?://\S+", text)
    if not m:
        return None
    url = m.group(0)
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "as-checkin-bot/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            url = r.geturl()
    except Exception:
        pass
    # Yandex: ll=lon,lat or pt=lon,lat
    m = _re.search(r"(?:ll|pt)=([-\d.]+)%?[2C]?[cC]?([-\d.]+)", url)
    if not m:
        m = _re.search(r"(?:ll|pt)=([-\d.]+),([-\d.]+)", url)
    if m:
        return float(m.group(2)), float(m.group(1))
    # Google: @lat,lon or q=lat,lon
    m = _re.search(r"@([-\d.]+),([-\d.]+)", url)
    if not m:
        m = _re.search(r"[?&](?:q|query|destination)=([-\d.]+),([-\d.]+)", url)
    if m:
        return float(m.group(1)), float(m.group(2))
    return None


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
        names, shot = await run_in_thread(device.venues_near, latitude, longitude,
                                 store.jitter_on())
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


def _place_card(place) -> tuple[str, InlineKeyboardMarkup, int]:
    places = store.list()
    idx = next((i for i, p in enumerate(places) if p.name == place.name), -1)
    services = "\n".join(f"  • {x}" for x in place.services) or "  —"
    text = (f"{place.name}\nАдрес: {place.address or '—'}\n"
            f"Координаты: {place.lat}, {place.lon}\nУслуги:\n{services}")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Услуги", callback_data=f"svcedit:{idx}"),
         InlineKeyboardButton(text="🗑 Удалить", callback_data=f"del:{idx}")],
        [InlineKeyboardButton(text="🏃 Запросить визит", callback_data=f"vis:{idx}")],
        [InlineKeyboardButton(text="← Назад к объектам", callback_data="plcback")],
    ])
    return text, kb, idx


@router.callback_query(F.data.startswith("plc:"))
async def callback_place_card(c: types.CallbackQuery):
    if c.from_user.id not in config.allowed_users:
        return await c.answer()
    try:
        place = _pending_places[c.from_user.id][int(c.data.removeprefix("plc:"))]
    except (KeyError, IndexError, ValueError):
        return await c.answer("Список устарел — открой «Объекты» заново.",
                              show_alert=True)
    place = store.get(place.name) or place  # fresh copy, services may differ
    text, kb, _ = _place_card(place)
    await c.message.edit_text(text, reply_markup=kb)
    await c.answer()


@router.callback_query(F.data == "plcback")
async def callback_places_back(c: types.CallbackQuery):
    if c.from_user.id not in config.allowed_users:
        return await c.answer()
    places = store.list()
    _pending_places[c.from_user.id] = places
    lines = [
        f"• {p.name}: {p.lat}, {p.lon}" + (f" — {p.address}" if p.address else "")
        for p in places
    ]
    buttons = [
        [InlineKeyboardButton(text=p.name, callback_data=f"plc:{i}")]
        for i, p in enumerate(places)
    ]
    await c.message.edit_text(
        "\n".join(lines) + "\n\nНажми на объект — откроются действия.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await c.answer()


@router.callback_query(F.data.startswith("vis:"))
async def callback_visit(c: types.CallbackQuery):
    if c.from_user.id not in config.allowed_users:
        return await c.answer()
    try:
        place = _pending_places[c.from_user.id][int(c.data.removeprefix("vis:"))]
    except (KeyError, IndexError, ValueError):
        return await c.answer("Список устарел — открой «Объекты» заново.",
                              show_alert=True)
    place = store.get(place.name) or place
    await c.answer()
    await _drop(c.message)
    await offer_services(c.message, c.from_user.id, place)


@router.callback_query(F.data.startswith("svcedit:"))
async def callback_edit_services(c: types.CallbackQuery):
    if c.from_user.id not in config.allowed_users:
        return await c.answer()
    try:
        place = _pending_places[c.from_user.id][int(c.data.removeprefix("svcedit:"))]
    except (KeyError, IndexError, ValueError):
        return await c.answer("Список устарел — открой «Объекты» заново.",
                              show_alert=True)
    await c.answer()
    prog = await c.message.edit_text(f"Собираю услуги «{place.name}»…")
    try:
        await run_in_thread(device.connect)
        services = await run_in_thread(
            device.services_of_venue, place.name, place.lat, place.lon,
            store.jitter_on())
    except Exception as e:
        log.exception("services_of_venue failed")
        return await prog.edit_text(f"Не удалось собрать услуги: {e}")
    if not services:
        return await prog.edit_text("Услуги не распарсились.")
    _pending_svc_pick[c.from_user.id] = {
        "place": (place.name, place.lat, place.lon, place.address),
        "services": services,
        "chosen": {i for i, s in enumerate(services) if s in place.services},
    }
    await _render_service_pick(prog, place.name, services,
                               _pending_svc_pick[c.from_user.id]["chosen"])


@router.callback_query(F.data.startswith("del:"))
async def callback_del_place(c: types.CallbackQuery):
    if c.from_user.id not in config.allowed_users:
        return await c.answer()
    try:
        place = _pending_places[c.from_user.id][int(c.data.removeprefix("del:"))]
        name = place.name
    except (KeyError, IndexError, ValueError):
        return await c.answer("Список устарел — открой «Объекты» заново.", show_alert=True)
    if store.remove(name):
        await c.answer(f"Удалено: {name}")
        await _drop(c.message)
        await c.message.answer(f"Removed {name}", reply_markup=main_menu())
    else:
        await c.answer("Уже удалён.", show_alert=True)


@router.callback_query(F.data.startswith("save:"))
async def callback_save_place(c: types.CallbackQuery):
    if c.from_user.id not in config.allowed_users:
        return await c.answer()
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
    await _drop(c.message)
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
        services = await run_in_thread(device.services_of_venue, name, lat, lon,
                                       store.jitter_on())
    except Exception as e:
        log.exception("services_of_venue failed")
        return await prog.edit_text(f"Не удалось собрать услуги: {e}")
    if not services:
        return await prog.edit_text("Услуги не распарсились.")

    _pending_svc_pick[c.from_user.id] = {
        "place": (name, lat, lon, detail),
        "services": services,
        "chosen": set(),
        "delete_on_cancel": True,  # registration in-flight → abort removes draft
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
    buttons.append([InlineKeyboardButton(text="Готово", callback_data="svcdone"),
                    InlineKeyboardButton(text="Отмена", callback_data="svccancel")])
    await prog.edit_text(
        f"Услуги «{venue}» — отметь нужные и жми «Готово»:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.callback_query(F.data.startswith("svcpick:"))
async def callback_svc_pick(c: types.CallbackQuery):
    if c.from_user.id not in config.allowed_users:
        return await c.answer()
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
        return await c.answer()
    pend = _pending_svc_pick.pop(c.from_user.id, None)
    if not pend:
        return await c.answer("Список устарел.", show_alert=True)
    name, lat, lon, detail = pend["place"]
    chosen = [s for i, s in enumerate(pend["services"]) if i in pend["chosen"]]
    store.add(name, lat, lon, address=detail, services=chosen)
    listing = "\n".join(f"• {s}" for s in chosen) or "— не выбраны"
    await c.message.edit_text(f"Услуги «{name}» сохранены:\n" + listing)
    await c.answer("Сохранено")


@router.callback_query(F.data == "svccancel")
async def callback_svc_cancel(c: types.CallbackQuery):
    if c.from_user.id not in config.allowed_users:
        return await c.answer()
    pend = _pending_svc_pick.pop(c.from_user.id, None)
    if not pend:
        return await c.answer("Список устарел.", show_alert=True)
    name = pend["place"][0]
    if pend.get("delete_on_cancel"):
        store.remove(name)
        await c.message.edit_text(f"Отменено — объект «{name}» не сохранён.")
    else:
        await c.message.edit_text(
            f"Отменено — услуги «{name}» остались без изменений.")
    await c.answer("Отменено")


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
        return await c.answer()
    place = _pending_svc.get(c.from_user.id)
    try:
        service = place.services[int(c.data.removeprefix("svc:"))]
    except (AttributeError, IndexError, ValueError):
        return await c.answer(
            "Список устарел — выбери объект заново.", show_alert=True
        )
    await c.answer(service)
    await _drop(c.message)
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
            on_step=on_step, service=service, jitter=store.jitter_on(),
        )
        sp = device.last_spoof
        if sp:
            geo = (f"📍 {sp[0]},{sp[1]} → {sp[2]:.6f},{sp[3]:.6f} "
                   f"(смещение {sp[4]:.0f} м)")
        else:
            geo = f"📍 {place.lat},{place.lon}"
        outcome = next(
            (s.name for s in reversed(steps) if s.name.startswith("outcome_")),
            "",
        )
        if outcome == "outcome_confirmed":
            await progress.edit_text(
                f"✅ Визит подтверждён администратором.\n{geo}")
        elif outcome == "outcome_expired":
            await progress.edit_text(
                "⏱ Время ожидания истекло — администратор не успел "
                f"подтвердить. Попробуй ещё раз.\n{geo}")
        else:
            await progress.edit_text(
                f"Request sent — waiting for admin confirmation.\n{geo}")
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
    keyboard = place_menu(m.from_user.id)
    if not keyboard:
        return await m.answer(
            "Объектов пока нет. Нажми «Добавить объект».",
            reply_markup=main_menu(),
        )
    await m.answer("Выбери объект:", reply_markup=keyboard)


@router.callback_query(F.data.startswith("checkin:"))
async def callback_checkin(c: types.CallbackQuery):
    if c.from_user.id not in config.allowed_users:
        return await c.answer()
    try:
        place = _pending_places[c.from_user.id][int(c.data.removeprefix("checkin:"))]
    except (KeyError, IndexError, ValueError):
        return await c.answer("Список устарел — нажми «Запросить визит» ещё раз.",
                              show_alert=True)
    place = store.get(place.name)
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
        "Можно ссылкой «Поделиться» из Яндекс/Google Карт "
        "или координатами: 53.915323, 27.571478",
        reply_markup=main_menu(),
    )


@router.message(Command("update"))
@router.message(F.text == BTN_UPDATE)
@owner_only
async def cmd_update(m: types.Message):
    """Self-update: git pull in the repo dir + restart the systemd unit."""
    import os

    await _drop(m)

    repo_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", repo_dir, "pull", "--ff-only",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    text = out.decode(errors="replace").strip()[:3500]
    m1 = await m.answer(f"```\n{text}\n```", parse_mode="Markdown")
    m2 = await m.answer(
        "Перезапускаю бота… (если он молчит пару минут — проверь journalctl)")
    _track(m.from_user.id, "update", m1.message_id, m2.message_id)
    await asyncio.create_subprocess_exec("systemctl", "restart", "asbot")


@router.message(Command("stop"))
@owner_only
async def cmd_stop(m: types.Message):
    await _drop(m)
    try:
        await run_in_thread(device.connect)
        await run_in_thread(device.stop_visit)
        await m.answer("Визит остановлен: геопозиция снята, Allsports закрыт",
                       reply_markup=main_menu())
    except DeviceError as e:
        await m.answer(f"Failed: {e}")


@router.message(Command("shot"))
@owner_only
async def cmd_shot(m: types.Message):
    await _drop(m)
    await _untrack(m.bot, m.from_user.id, "shot")
    try:
        await run_in_thread(device.connect)
        png = await run_in_thread(device.screenshot)
        r = await m.answer_photo(BufferedInputFile(png, filename="screen.png"))
        _track(m.from_user.id, "shot", r.message_id)
    except DeviceError as e:
        await m.answer(f"Failed: {e}")


@router.message(Command("dump"))
@owner_only
async def cmd_dump(m: types.Message):
    await _drop(m)
    await _untrack(m.bot, m.from_user.id, "dump")
    try:
        await run_in_thread(device.connect)
        xml = await run_in_thread(device.dump_ui)
        r = await m.answer_document(
            BufferedInputFile(xml.encode(), filename="hierarchy.xml"),
            caption="UI hierarchy",
        )
        _track(m.from_user.id, "dump", r.message_id)
    except Exception as e:
        log.exception("dump failed")
        await m.answer(f"Failed: {e}")


@router.message(Command("status"))
@owner_only
async def cmd_status(m: types.Message):
    await _drop(m)
    await _untrack(m.bot, m.from_user.id, "status")
    try:
        await run_in_thread(device.connect)
        app = await run_in_thread(device.current_app)
        r = await m.answer(f"Device OK\nForeground: {app}",
                           reply_markup=main_menu())
    except DeviceError as e:
        r = await m.answer(f"Device unreachable: {e}", reply_markup=main_menu())
    _track(m.from_user.id, "status", r.message_id)


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


def _settings_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="Смещение координат: "
                 + ("вкл (10-30 м)" if store.jitter_on() else "выкл (точная точка)"),
            callback_data="setjitter")],
        [InlineKeyboardButton(text="✕ Закрыть", callback_data="setclose")],
    ])


@router.message(F.text == BTN_SETTINGS)
@router.message(Command("settings"))
@owner_only
async def menu_settings(m: types.Message):
    await _drop(m)
    await _untrack(m.bot, m.from_user.id, "settings")
    r = await m.answer("Настройки:", reply_markup=_settings_kb())
    _track(m.from_user.id, "settings", r.message_id)


@router.callback_query(F.data == "setjitter")
async def callback_set_jitter(c: types.CallbackQuery):
    if c.from_user.id not in config.allowed_users:
        return await c.answer()
    on = store.jitter_on()
    store.set_setting("jitter", "0" if on else "1")
    await c.message.edit_text("Настройки:", reply_markup=_settings_kb())
    await c.answer("Смещение: " + ("выкл" if on else "вкл"))


@router.callback_query(F.data == "setclose")
async def callback_set_close(c: types.CallbackQuery):
    if c.from_user.id not in config.allowed_users:
        return await c.answer()
    await _drop(c.message)
    await c.answer()


@router.message(F.text == BTN_DUMP)
@owner_only
async def menu_dump(m: types.Message):
    await cmd_dump(m)


@router.callback_query(F.data.startswith("geo:"))
async def callback_geo_pick(c: types.CallbackQuery):
    if c.from_user.id not in config.allowed_users:
        return await c.answer()
    try:
        lat, lon, disp = _pending_geo[c.from_user.id][int(c.data.removeprefix("geo:"))]
    except (KeyError, IndexError, ValueError):
        return await c.answer("Список устарел — нажми «Добавить объект» заново.",
                              show_alert=True)
    _awaiting_place_name.discard(c.from_user.id)
    await c.answer(disp.split(",")[0])
    await _drop(c.message)
    await discover_venues(c.message, c.from_user.id, "", lat, lon)


@router.message(F.text)
async def free_text(m: types.Message):
    uid = m.from_user.id
    if uid not in _awaiting_place_name or m.from_user.id not in config.allowed_users:
        return
    query = m.text.strip()
    parts = query.replace(",", " ").split()
    try:
        lat, lon = float(parts[-2]), float(parts[-1])
        _awaiting_place_name.discard(uid)
        return await discover_venues(m, uid, "", lat, lon)
    except (ValueError, IndexError):
        pass
    if "http" in query:
        wait = await m.answer("Вытаскиваю координаты из ссылки…")
        found_ll = await run_in_thread(coords_from_link, query)
        if found_ll:
            _awaiting_place_name.discard(uid)
            lat, lon = found_ll
            await wait.edit_text(f"Координаты из ссылки: {lat}, {lon}")
            return await discover_venues(m, uid, "", lat, lon)
        return await wait.edit_text(
            "Не вытащил координаты из ссылки. Попробуй название или "
            "координаты текстом.")
    wait = await m.answer(f"Ищу «{query}» на карте…")
    try:
        found = await run_in_thread(geocode, query)
        if not found and "," not in query:
            found = await run_in_thread(geocode, f"{query}, Минск")
    except Exception as e:
        log.exception("geocode failed")
        return await wait.edit_text(f"Геокодер недоступен: {e}")
    if not found:
        return await wait.edit_text(
            f"Не нашёл «{query}» на карте.\n"
            "Попробуй другое название, пришли ссылку «Поделиться» "
            "из Яндекс/Google Карт или координаты текстом."
        )
    _pending_geo[uid] = found
    buttons = [
        [InlineKeyboardButton(text=disp[:64], callback_data=f"geo:{i}")]
        for i, (_lat, _lon, disp) in enumerate(found)
    ]
    await wait.edit_text(
        "Выбери нужный вариант — или напиши другое название:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
