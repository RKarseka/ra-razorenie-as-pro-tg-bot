"""Telegram bot handlers."""

import asyncio
import logging
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps
from types import SimpleNamespace

from aiogram import Router, types, F
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from .config import config
from .device import Device, DeviceError, CheckinCancelled
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
_awaiting_name_for: dict[int, int] = {}
_awaiting_balance: dict[int, tuple[str, str]] = {}  # uid -> (venue, op)


def _fmt_min(minutes: int) -> str:
    sign = "−" if minutes < 0 else ""
    h, mm = divmod(abs(minutes), 60)
    if h and mm:
        return f"{sign}{h} ч {mm} мин"
    return f"{sign}{h} ч" if h else f"{sign}{mm} мин"


def _service_minutes(service: str | None) -> int:
    if service:
        import re as _re
        mm = _re.search(r"(\d+)\s*мин", service)
        if mm:
            return int(mm.group(1))
    return 60


def _parse_hours(text: str) -> int | None:
    """'1.5'/'2'/'1,5' → hours; '45 мин' → minutes. Returns minutes."""
    s = text.strip().lower().replace(",", ".")
    try:
        if s.endswith("мин"):
            return int(round(float(s[:-3].strip())))
        return int(round(float(s) * 60))
    except ValueError:
        return None
# Geocoding candidates offered to each user.
_pending_geo: dict[int, list] = {}
# Live visit requests awaiting the admin decision, keyed by request id.
_pending_req: dict[str, dict] = {}
_checkin_lock = asyncio.Lock()
# Admin restart picks when several requests are open.
_pending_restart: dict[int, list] = {}

BTN_CHECKIN = "Запросить визит"
BTN_PLACES = "Объекты"
BTN_ADD_PLACE = "Добавить объект"
BTN_SHOT = "Скриншот"
BTN_STATUS = "Статус"
BTN_STOP = "Остановить визит"
BTN_DUMP = "Дамп UI"
BTN_UPDATE = "Обновить бота"
BTN_SETTINGS = "Настройки"
BTN_FORCE = "▶️ Запустить сразу"
BTN_ADM_RESTART = "Перезапустить запрос"


def main_menu() -> ReplyKeyboardMarkup:
    row1 = []
    req = store.current_request()
    if req:
        row1.append(KeyboardButton(text=f"⏳ {req['venue']}"))
        pend = _pending_req.get(req["id"])
        if req["state"] == "waiting" and pend and pend["result"] is None:
            row1.append(KeyboardButton(text=BTN_FORCE))
    row1.append(KeyboardButton(text=BTN_CHECKIN))
    return ReplyKeyboardMarkup(
        keyboard=[
            row1,
            [KeyboardButton(text=BTN_STOP), KeyboardButton(text=BTN_PLACES)],
            [
                KeyboardButton(text=BTN_SHOT),
                KeyboardButton(text=BTN_UPDATE),
                KeyboardButton(text=BTN_SETTINGS),
            ],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def admin_menu() -> ReplyKeyboardRemove:
    return ReplyKeyboardRemove()


_TZ3 = timezone(timedelta(hours=3))


def _secs_until_close() -> float:
    """Seconds until 22:30 today (UTC+3). 0 when already past."""
    now = datetime.now(_TZ3)
    deadline = now.replace(hour=22, minute=30, second=0, microsecond=0)
    return max(0.0, (deadline - now).total_seconds())


async def _display_name(bot, tg_id: int) -> str:
    """Owner-defined alias first, then the Telegram profile name."""
    alias = store.name_of(tg_id)
    if alias:
        return alias
    try:
        ch = await bot.get_chat(tg_id)
        return ("@" + ch.username) if ch.username else (ch.full_name or str(tg_id))
    except Exception:
        return str(tg_id)


async def _broadcast(bot, venue: str, owner_uid: int, text: str,
                     exclude: set[int] | None = None,
                     reply_markup=None) -> list[types.Message]:
    """Log an event to the request owner AND every venue admin at once."""
    targets = ({owner_uid} | set(store.admins_for(venue))) - (exclude or set())
    sent = []
    for uid in targets:
        try:
            msg = await bot.send_message(uid, text,
                                         reply_markup=reply_markup)
            sent.append(msg)
            if uid != owner_uid:  # admin chats self-clean after 20 min
                asyncio.create_task(_autodelete(msg, 1200))
        except Exception:
            log.exception("broadcast to %s failed", uid)
    return sent


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


async def _autodelete(m: types.Message, secs: int = 20) -> None:
    await asyncio.sleep(secs)
    await _drop(m)


async def _send_menu(m: types.Message, uid: int) -> None:
    """Re-anchor the reply keyboard: deleting the message that last
    carried reply_markup hides the keyboard, so keep it on one
    persistent 'menu' message — resend it fresh, track, untrack old."""
    await _untrack(m.bot, uid, "menu")
    r = await m.answer("Выбери действие кнопкой ниже.",
                       reply_markup=main_menu())
    _track(uid, "menu", r.message_id)


def _track(uid: int, kind: str, *ids: int) -> None:
    """Persist transient message ids so they can be deleted later,
    including after a restart."""
    store.set_setting(f"trk:{uid}:{kind}", ",".join(str(i) for i in ids if i))


def _track_more(uid: int, kind: str, *ids: int) -> None:
    """Append to the tracked transient ids of a kind (unlike _track)."""
    raw = store.get_setting(f"trk:{uid}:{kind}", "")
    cur = [s for s in raw.split(",") if s]
    store.set_setting(f"trk:{uid}:{kind}", ",".join(cur + [str(i) for i in ids if i]))


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
async def cmd_help(m: types.Message, command: CommandObject):
    await _drop(m)
    uid = m.from_user.id
    if command.args and command.args.startswith("adm_"):
        await _accept_invite(m, command.args[4:])
        return
    if uid not in config.allowed_users:
        if store.is_admin(uid):
            venues = ", ".join(store.venues_of(uid))
            await _untrack(m.bot, uid, "menu")
            r = await m.answer(f"Привет, администратор {venues}!",
                               reply_markup=admin_menu())
            _track(uid, "menu", r.message_id)
        return  # silently ignore strangers
    await _send_menu(m, uid)


async def _accept_invite(m: types.Message, token: str) -> None:
    row = store.take_invite(token)
    if not row:
        return await m.answer("Ссылка недействительна или уже использована.")
    venue, invited_by = row
    store.add_admin(venue, m.from_user.id)
    who = await _display_name(m.bot, m.from_user.id)
    r = await m.answer(f"Ты принял приглашение — теперь ты администратор "
                       f"«{venue}».\nЗапросы на визит будут приходить сюда.",
                       reply_markup=admin_menu())
    asyncio.create_task(_autodelete(r, 1200))
    try:
        r = await m.bot.send_message(invited_by,
                                     f"✅ {who} принял приглашение и стал "
                                     f"администратором «{venue}».")
        _track_more(invited_by, "settings", r.message_id)
        asyncio.create_task(_autodelete(r, 1200))
    except Exception:
        pass


@router.message(Command("places"))
@owner_only
async def cmd_places(m: types.Message):
    await _drop(m)
    places = store.list()
    if not places:
        return await m.answer(
            "Объектов пока нет.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="➕ Добавить объект",
                                     callback_data="addplc")]]))
    lines = [
        f"• {p.name}: {p.lat}, {p.lon}" + (f" — {p.address}" if p.address else "")
        for p in places
    ]
    _pending_places[m.from_user.id] = places
    buttons = [
        [InlineKeyboardButton(text=p.name, callback_data=f"plc:{i}")]
        for i, p in enumerate(places)
    ]
    buttons.append([InlineKeyboardButton(text="➕ Добавить объект",
                                         callback_data="addplc")])
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
    await _drop(m)
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
        try:
            await progress.edit_text(f"Не удалось открыть список: {e}")
        except Exception:
            await m.answer(f"Не удалось открыть список: {e}")


def _place_card(place) -> tuple[str, InlineKeyboardMarkup, int]:
    places = store.list()
    idx = next((i for i, p in enumerate(places) if p.name == place.name), -1)
    services = "\n".join(f"  • {x}" for x in place.services) or "  —"
    bal = (f"💳 Баланс: {_fmt_min(place.balance_min)}\n"
           if place.balance_track else "")
    text = (f"{place.name}\nАдрес: {place.address or '—'}\n"
            f"Координаты: {place.lat}, {place.lon}\n{bal}Услуги:\n{services}")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Услуги", callback_data=f"svcedit:{idx}"),
         InlineKeyboardButton(text="🗑 Удалить", callback_data=f"del:{idx}")],
        [InlineKeyboardButton(text="🏃 Запросить визит", callback_data=f"vis:{idx}")],
        [InlineKeyboardButton(text="⏱ Баланс часов", callback_data=f"bal:{idx}"),
         InlineKeyboardButton(text="👥 Администраторы", callback_data=f"admlist:{idx}")],
        [InlineKeyboardButton(text="← Назад к объектам", callback_data="plcback")],
    ])
    return text, kb, idx


def _balance_card(place) -> tuple[str, InlineKeyboardMarkup]:
    places = store.list()
    idx = next((i for i, p in enumerate(places) if p.name == place.name), -1)
    text = (f"⏱ Баланс «{place.name}»: {_fmt_min(place.balance_min)}\n"
            f"Учёт часов: {'вкл' if place.balance_track else 'выкл'}")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Пополнить",
                              callback_data=f"balop:{idx}:add"),
         InlineKeyboardButton(text="➖ Списать",
                              callback_data=f"balop:{idx}:sub")],
        [InlineKeyboardButton(text="✏️ Установить значение",
                              callback_data=f"balop:{idx}:set")],
        [InlineKeyboardButton(
            text="🔕 Выключить учёт" if place.balance_track
            else "🔔 Включить учёт",
            callback_data=f"baltoggle:{idx}")],
        [InlineKeyboardButton(text="← Назад к объекту",
                              callback_data=f"plc:{idx}")],
    ])
    return text, kb


def _resolve_place(c: types.CallbackQuery, prefix: str):
    try:
        return _pending_places[c.from_user.id][int(prefix)]
    except (KeyError, IndexError, ValueError):
        return None


async def _apply_balance(bot, uid: int, venue: str, op: str,
                         minutes: int) -> str:
    place = store.get(venue)
    if not place:
        return f"Объект «{venue}» не найден."
    if not place.balance_track:
        return f"Учёт часов для «{venue}» выключен — включи его в «Баланс часов»."
    if op == "add":
        nb = store.add_balance(venue, minutes)
        store.log_balance(venue, minutes, "manual")
        return f"💳 +{_fmt_min(minutes)} → баланс «{venue}»: {_fmt_min(nb)}"
    if op == "sub":
        nb = store.add_balance(venue, -minutes)
        store.log_balance(venue, -minutes, "manual")
        await _broadcast(
            bot, venue, uid,
            f"⏱ Списание по «{venue}»: −{_fmt_min(minutes)}. "
            f"Остаток: {_fmt_min(nb)}.",
            exclude={uid})
        return (f"💳 −{_fmt_min(minutes)} → баланс «{venue}»: "
                f"{_fmt_min(nb)}\nАдминистраторы уведомлены.")
    prev = place.balance_min
    store.set_balance(venue, minutes)
    store.log_balance(venue, minutes - prev, "correct")
    return f"💳 Баланс «{venue}» установлен: {_fmt_min(minutes)}"


@router.callback_query(F.data.startswith("bal:"))
async def callback_balance(c: types.CallbackQuery):
    if c.from_user.id not in config.allowed_users:
        return await c.answer()
    place = _resolve_place(c, c.data.removeprefix("bal:"))
    if place is None:
        return await c.answer("Список устарел — открой «Объекты» заново.",
                              show_alert=True)
    place = store.get(place.name) or place
    text, kb = _balance_card(place)
    await c.message.edit_text(text, reply_markup=kb)
    await c.answer()


@router.callback_query(F.data.startswith("baltoggle:"))
async def callback_balance_toggle(c: types.CallbackQuery):
    if c.from_user.id not in config.allowed_users:
        return await c.answer()
    place = _resolve_place(c, c.data.removeprefix("baltoggle:"))
    if place is None:
        return await c.answer("Список устарел — открой «Объекты» заново.",
                              show_alert=True)
    cur = store.get(place.name) or place
    store.set_balance_track(place.name, not cur.balance_track)
    place = store.get(place.name) or cur
    text, kb = _balance_card(place)
    await c.message.edit_text(text, reply_markup=kb)
    await c.answer(
        "Учёт включён" if place.balance_track else "Учёт выключен")


_BAL_OPS = {"add": "Пополнение", "sub": "Списание", "set": "Установка баланса"}


@router.callback_query(F.data.startswith("balop:"))
async def callback_balance_op(c: types.CallbackQuery):
    if c.from_user.id not in config.allowed_users:
        return await c.answer()
    try:
        _p, idx_s, op = c.data.split(":")
        place = _pending_places[c.from_user.id][int(idx_s)]
    except (KeyError, IndexError, ValueError):
        return await c.answer("Список устарел — открой «Объекты» заново.",
                              show_alert=True)
    if op not in _BAL_OPS:
        return await c.answer()
    rows = [[InlineKeyboardButton(
        text=f"{label} ч", callback_data=f"baldo:{idx_s}:{op}:{mins}")]
        for label, mins in (("0.5", 30), ("1", 60), ("1.5", 90), ("2", 120))]
    rows.append([InlineKeyboardButton(text="⌨️ Своё значение",
                                      callback_data=f"balcust:{idx_s}:{op}")])
    rows.append([InlineKeyboardButton(text="← Назад",
                                      callback_data=f"bal:{idx_s}")])
    await c.message.edit_text(
        f"{_BAL_OPS[op]} — «{place.name}».\nСколько часов?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await c.answer()


@router.callback_query(F.data.startswith("baldo:"))
async def callback_balance_do(c: types.CallbackQuery):
    if c.from_user.id not in config.allowed_users:
        return await c.answer()
    try:
        _p, idx_s, op, min_s = c.data.split(":")
        place = _pending_places[c.from_user.id][int(idx_s)]
        minutes = int(min_s)
    except (KeyError, IndexError, ValueError):
        return await c.answer("Данные устарели — открой «Объекты» заново.",
                              show_alert=True)
    text = await _apply_balance(c.bot, c.from_user.id, place.name, op, minutes)
    place = store.get(place.name) or place
    card, kb = _balance_card(place)
    await c.message.edit_text(f"{text}\n\n{card}", reply_markup=kb)
    await c.answer()


@router.callback_query(F.data.startswith("balcust:"))
async def callback_balance_custom(c: types.CallbackQuery):
    if c.from_user.id not in config.allowed_users:
        return await c.answer()
    try:
        _p, idx_s, op = c.data.split(":")
        place = _pending_places[c.from_user.id][int(idx_s)]
    except (KeyError, IndexError, ValueError):
        return await c.answer("Список устарел — открой «Объекты» заново.",
                              show_alert=True)
    _awaiting_balance[c.from_user.id] = (place.name, op)
    await c.message.edit_text(
        f"{_BAL_OPS.get(op, 'Сумма')} — «{place.name}».\n"
        "Напиши количество часов (например, 1.5 или 2):")
    await c.answer()


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
    buttons.append([InlineKeyboardButton(text="➕ Добавить объект",
                                         callback_data="addplc")])
    await c.message.edit_text(
        "\n".join(lines) + "\n\nНажми на объект — откроются действия.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await c.answer()


@router.callback_query(F.data == "addplc")
async def callback_add_place(c: types.CallbackQuery):
    if c.from_user.id not in config.allowed_users:
        return await c.answer()
    _awaiting_place_name.add(c.from_user.id)
    await c.answer()
    await c.message.answer(
        "Напиши название заведения — найду его на карте и предложу варианты.\n"
        "Можно ссылкой «Поделиться» из Яндекс/Google Карт "
        "или координатами: 53.915323, 27.571478",
        reply_markup=main_menu(),
    )


@router.callback_query(F.data.startswith("admlist:"))
async def callback_admins(c: types.CallbackQuery):
    if c.from_user.id not in config.allowed_users:
        return await c.answer()
    try:
        place = _pending_places[c.from_user.id][int(c.data.removeprefix("admlist:"))]
    except (KeyError, IndexError, ValueError):
        return await c.answer("Список устарел — открой «Объекты» заново.",
                              show_alert=True)
    idx = int(c.data.removeprefix("admlist:"))
    admins = store.admins_for(place.name)
    rows = []
    kb = []
    for i, aid in enumerate(admins):
        name = await _display_name(c.bot, aid)
        rows.append(f"• {name}")
        kb.append([InlineKeyboardButton(
            text=f"✏️ {name}", callback_data=f"admname:{idx}:{i}"),
            InlineKeyboardButton(
            text="🗑", callback_data=f"admdel:{idx}:{i}")])
    kb.append([InlineKeyboardButton(text="➕ Пригласить админа",
                                    callback_data=f"adminv:{idx}")])
    kb.append([InlineKeyboardButton(text="← Назад к объекту",
                                    callback_data=f"plc:{idx}")])
    _pending_places[c.from_user.id] = store.list()
    text = (f"👥 Администраторы «{place.name}»:\n"
            + ("\n".join(rows) if rows else "—"))
    await c.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await c.answer()


@router.callback_query(F.data.startswith("adminv:"))
async def callback_admin_invite(c: types.CallbackQuery):
    if c.from_user.id not in config.allowed_users:
        return await c.answer()
    try:
        place = _pending_places[c.from_user.id][int(c.data.removeprefix("adminv:"))]
    except (KeyError, IndexError, ValueError):
        return await c.answer("Список устарел — открой «Объекты» заново.",
                              show_alert=True)
    token = secrets.token_hex(4)
    store.add_invite(token, place.name, c.from_user.id)
    me = await c.bot.get_me()
    link = f"https://t.me/{me.username}?start=adm_{token}"
    await c.message.edit_text(
        f"Привет! Приглашаю тебя быть администратором «{place.name}».\n\n"
        f"Перейди по [ссылке]({link}) и нажми «Старт» — бот будет присылать "
        "запросы на отметку посещения, а тебе останется только подтвердить "
        "их в приложении.\n\n"
        "Ссылка одноразовая — перешли это сообщение админу целиком",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="← Назад", callback_data=f"admlist:{int(c.data.removeprefix('adminv:'))}")]]))
    _track_more(c.message.chat.id, "settings", c.message.message_id)
    await c.answer("Приглашение отправлено — перешли ссылку админу.")


@router.callback_query(F.data.startswith("admname:"))
async def callback_admin_rename(c: types.CallbackQuery):
    if c.from_user.id not in config.allowed_users:
        return await c.answer()
    try:
        _idx_s, aid_i = c.data.removeprefix("admname:").split(":", 1)
        place = _pending_places[c.from_user.id][int(_idx_s)]
        aid = store.admins_for(place.name)[int(aid_i)]
    except (KeyError, IndexError, ValueError):
        return await c.answer("Список устарел — открой «Объекты» заново.",
                              show_alert=True)
    _awaiting_name_for[c.from_user.id] = aid
    cur = store.name_of(aid)
    await c.message.answer(
        f"Напиши псевдоним для {await _display_name(c.bot, aid)}"
        + (f" (сейчас: {cur})" if cur else "") + ":")
    await c.answer()


@router.callback_query(F.data == "setname")
async def callback_set_own_name(c: types.CallbackQuery):
    if c.from_user.id not in config.allowed_users:
        return await c.answer()
    _awaiting_name_for[c.from_user.id] = c.from_user.id
    cur = store.name_of(c.from_user.id)
    await c.message.answer(
        "Напиши свой псевдоним — его будут видеть администраторы"
        + (f" (сейчас: {cur})" if cur else "") + ":")
    await c.answer()


@router.callback_query(F.data.startswith("admdel:"))
async def callback_admin_del(c: types.CallbackQuery):
    if c.from_user.id not in config.allowed_users:
        return await c.answer()
    try:
        _idx_s, aid_i = c.data.removeprefix("admdel:").split(":", 1)
        idx, aid_i = int(_idx_s), int(aid_i)
        place = _pending_places[c.from_user.id][idx]
        aid = store.admins_for(place.name)[aid_i]
    except (KeyError, IndexError, ValueError):
        return await c.answer("Список устарел — открой «Объекты» заново.",
                              show_alert=True)
    store.del_admin(place.name, aid)
    await c.answer("Администратор удалён.")
    c.data = f"admlist:{idx}"
    await callback_admins(c)


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
    await _drop(m)
    if not command.args or not store.remove(command.args.strip()):
        return await m.answer("Not found. /places to list", reply_markup=main_menu())
    await m.answer("Removed", reply_markup=main_menu())


@router.message(Command("checkin"))
@owner_only
async def cmd_checkin(m: types.Message, command: CommandObject):
    await _drop(m)
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
    uid = m.chat.id
    admins = store.admins_for(place.name)
    if admins:
        await _request_admin_ready(m, place, service, admins)
        return
    await _run_checkin(m, place, service)


def _req_kb(req_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Готов", callback_data=f"reqok:{req_id}"),
        InlineKeyboardButton(text="✋ Не могу", callback_data=f"reqno:{req_id}"),
    ]])


async def _request_admin_ready(m, place, service, admins) -> None:
    """Ping every venue admin; run the check-in once one confirms
    or the owner force-starts. Wait until 22:30 (UTC+3)."""
    uid = m.chat.id
    req_id = secrets.token_hex(4)
    svc = f" · {service}" if service else ""
    store.close_open_requests()
    store.put_request(req_id, place.name, service or "", uid, "waiting")
    _pending_req[req_id] = {"event": asyncio.Event(), "result": None,
                            "declined": set(), "owner": uid}

    kb = _req_kb(req_id)
    # m.from_user can be the bot itself when the flow started from a
    # callback on a bot message — resolve the owner's profile instead.
    owner_name = await _display_name(m.bot, uid)
    what = service or place.name
    text = (f"Привет. Давай отметим «{what}» от {owner_name}.\n"
            f"Заведение: «{place.name}» — готов подтвердить?")
    for aid in admins:
        try:
            await _untrack(m.bot, aid, "admreq")
            r = await m.bot.send_message(aid, text, reply_markup=kb)
            _track(aid, "admreq", r.message_id)
            asyncio.create_task(_autodelete(r, 1200))
        except Exception:
            pass
    # Admins already have the detailed ping above — no extra 📨 copy.
    # One message carries the keyboard; the force-launch action sits
    # in the keyboard itself while the request is waiting.
    await _untrack(m.bot, uid, "req")
    r = await m.answer(
        f"📨 Запрос отправлен администраторам «{place.name}»{svc}.\n"
        "⏳ Жду готовности до 22:30.",
        reply_markup=main_menu())
    _track_more(uid, "req", r.message_id)

    wait_s = _secs_until_close()
    try:
        try:
            await asyncio.wait_for(
                _pending_req[req_id]["event"].wait(), timeout=wait_s or 1)
            result = _pending_req[req_id]["result"]
        except asyncio.TimeoutError:
            result = "timeout"
    finally:
        _pending_req.pop(req_id, None)
    for aid in admins:
        try:
            await _untrack(m.bot, aid, "admreq")
        except Exception:
            pass
    await _drop(r)  # the waiting message is stale in every outcome

    if result in ("ready", "force"):
        if result == "ready":
            rr = await m.answer(f"✅ Администратор готов — запускаю "
                                f"«{place.name}»{svc}.")
            _track_more(uid, "req", rr.message_id)
            await _broadcast(m.bot, place.name, uid,
                             f"✅ Администратор готов — запускаю "
                             f"«{place.name}»{svc}.", exclude={uid})
        else:
            await _broadcast(m.bot, place.name, uid,
                             f"▶️ Запуск без подтверждения администратора "
                             f"— «{place.name}»{svc}.", exclude={uid})
        await _run_checkin(m, place, service, req_id)
        return

    try:
        store.set_request_state(req_id, "closed")
    except Exception:
        log.exception("set_request_state failed")
    if result == "cancelled":
        sent = await _broadcast(m.bot, place.name, uid,
                                f"⛔ Запрос в «{place.name}»{svc} отменён.",
                                exclude={uid})
        for msg in sent:
            asyncio.create_task(_autodelete(msg, 120))
    elif result == "declined":
        rr = await m.answer(f"Администраторы «{place.name}» не могут принять "
                            "запрос сейчас.", reply_markup=main_menu())
        _track_more(uid, "req", rr.message_id)
    else:
        rr = await m.answer(f"⏱ Не дождался готовности администратора "
                            f"«{place.name}» до 22:30 — запрос закрыт.",
                            reply_markup=main_menu())
        _track_more(uid, "req", rr.message_id)
        await _broadcast(m.bot, place.name, uid,
                         f"⏱ Запрос в «{place.name}»{svc} истёк "
                         "без подтверждения.", exclude={uid})


@router.callback_query(F.data.startswith("reqok:"))
async def callback_req_ready(c: types.CallbackQuery):
    req_id = c.data.removeprefix("reqok:")
    req = store.get_request(req_id)
    pend = _pending_req.get(req_id)
    if not req or not pend or c.from_user.id not in store.admins_for(
            req["venue"]):
        return await c.answer("Запрос устарел.", show_alert=True)
    pend["result"] = "ready"
    pend["event"].set()
    store.set_request_state(req_id, "ready")
    await c.answer("Принято — запускаю запрос.")
    await c.message.edit_text(c.message.text + "\n\n✅ Ты подтвердил готовность.")


@router.callback_query(F.data.startswith("reqno:"))
async def callback_req_decline(c: types.CallbackQuery):
    req_id = c.data.removeprefix("reqno:")
    req = store.get_request(req_id)
    pend = _pending_req.get(req_id)
    if not req or not pend or c.from_user.id not in store.admins_for(req["venue"]):
        return await c.answer("Запрос устарел.", show_alert=True)
    pend["declined"].add(c.from_user.id)
    await c.answer("Отклонено.")
    await c.message.edit_text(c.message.text + "\n\n✋ Ты отклонил.")
    await _broadcast(c.bot, req["venue"], req["owner"],
                     f"✋ Администратор {await _display_name(c.bot, c.from_user.id)} "
                     f"не может принять «{req['venue']}».")
    if pend["declined"] >= set(store.admins_for(req["venue"])):
        pend["result"] = "declined"
        pend["event"].set()


@router.message(F.text == BTN_FORCE)
@owner_only
async def menu_force(m: types.Message):
    await _drop(m)
    for pend in _pending_req.values():
        if pend["owner"] == m.from_user.id and pend["result"] is None:
            pend["result"] = "force"
            pend["event"].set()
            return


@router.callback_query(F.data.startswith("reqgo:"))
async def callback_req_force(c: types.CallbackQuery):
    if c.from_user.id not in config.allowed_users:
        return await c.answer()
    req_id = c.data.removeprefix("reqgo:")
    pend = _pending_req.get(req_id)
    if not pend or pend["owner"] != c.from_user.id:
        return await c.answer("Запрос устарел.", show_alert=True)
    pend["result"] = "force"
    pend["event"].set()
    await c.answer("Запускаю без подтверждения.")
    await _drop(c.message)


def _retry_kb(req_id: str | None, state: str):
    """Inline «Повторить запрос» for admins on expired/failed outcomes."""
    if req_id and state in ("expired", "failed"):
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔁 Повторить запрос",
                                 callback_data=f"retry:{req_id}")]])
    return None


@router.callback_query(F.data.startswith("retry:"))
async def callback_req_retry(c: types.CallbackQuery):
    req_id = c.data.removeprefix("retry:")
    req = store.get_request(req_id)
    if (not req or req["state"] not in ("expired", "failed")
            or c.from_user.id not in store.admins_for(req["venue"])):
        return await c.answer("Запрос устарел.", show_alert=True)
    place = store.get(req["venue"])
    if not place:
        return await c.answer("Объект удалён из списка.", show_alert=True)
    await c.answer("Повторяю запрос.")
    await _admin_restart(c.message, req)


async def _notify_request_sent(bot, venue: str, owner: int) -> None:
    await _broadcast(
        bot, venue, owner,
        f"📲 Запрос в «{venue}» отправлен — подтверди его в приложении.")


async def _remind_admin(bot, req_id: str) -> None:
    """One re-ping 10 min after an expired/failed request — only once;
    a new reminder is scheduled only by the next run's outcome."""
    await asyncio.sleep(600)
    req = store.get_request(req_id)
    if not req or req["state"] not in ("expired", "failed"):
        return
    for aid in store.admins_for(req["venue"]):
        try:
            await _untrack(bot, aid, "admreq")
            r = await bot.send_message(
                aid,
                f"⏰ Напоминание: визит в «{req['venue']}» не принят — "
                "подтверди его в приложении или сообщи владельцу.",
                reply_markup=admin_menu())
            _track(aid, "admreq", r.message_id)
            asyncio.create_task(_autodelete(r, 1200))
        except Exception:
            pass


async def _run_checkin(m: types.Message, place, service: str | None = None,
                       req_id: str | None = None):
    if _checkin_lock.locked():
        # A newer request supersedes the running visit: it aborts at
        # the next wait point, cleans up and releases the lock.
        device.cancel()
        r = await m.answer("⏳ Прерываю текущий заезд — запускаю новый…",
                           reply_markup=main_menu())
        _track_more(m.chat.id, "req", r.message_id)
    async with _checkin_lock:
        await _checkin_locked(m, place, service, req_id)


async def _checkin_locked(m: types.Message, place, service: str | None = None,
                          req_id: str | None = None):
    uid = m.chat.id
    await _untrack(m.bot, uid, "checkin")
    progress = await m.answer(f"Connecting to device at {config.device_serial}…",
                              reply_markup=main_menu())
    _track(uid, "checkin", progress.message_id)
    loop = asyncio.get_running_loop()
    last_step = ["connect"]

    async def _set_progress(text: str) -> None:
        try:
            await progress.edit_text(text)
        except Exception:
            pass

    def on_step(name: str) -> None:
        last_step[0] = name
        if name.startswith("restart_app"):
            # Surface the app restart in chat, not only in the log.
            n = name.rsplit("_", 1)[-1]
            label = f"перезапускаю приложение ({n}/3)"

            async def _note():
                r = await m.answer(
                    f"⚠️ «{place.name}»: список пуст или объект не найден "
                    f"— {label}")
                _track_more(uid, "req", r.message_id)
            asyncio.run_coroutine_threadsafe(_note(), loop)
        else:
            label = name
        asyncio.run_coroutine_threadsafe(
            _set_progress(f"{place.name}: {label}…"), loop
        )
        if name == "await_outcome":
            asyncio.run_coroutine_threadsafe(
                _notify_request_sent(m.bot, place.name, uid), loop
            )

    try:
        log.info("checkin: connecting to device")
        await asyncio.wait_for(run_in_thread(device.connect), timeout=45)
        log.info("checkin: connected, starting checkin flow")
        await _set_progress(f"Spoofing to {place.name} ({place.lat}, {place.lon})…")
        steps = await asyncio.wait_for(
            run_in_thread(
                device.checkin, place.name, place.lat, place.lon,
                on_step=on_step, service=service, jitter=store.jitter_on(),
            ),
            timeout=300,
        )
        log.info("checkin: flow finished")
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
            final = f"✅ Визит подтверждён администратором.\n{geo}"
            state = "confirmed"
            if place.balance_track:
                mins = _service_minutes(service)
                nb = store.add_balance(place.name, mins)
                store.log_balance(place.name, mins, "visit")
                final += f"\n💳 +{_fmt_min(mins)} → баланс: {_fmt_min(nb)}"
        elif outcome == "outcome_expired":
            final = ("⏱ Время ожидания истекло — администратор не успел "
                     f"подтвердить. Попробуй ещё раз.\n{geo}")
            state = "expired"
        else:
            final = f"Request sent — waiting for admin confirmation.\n{geo}"
            state = "sent"
        await _set_progress(final)
        if req_id:
            store.set_request_state(req_id, state)
        # Persistent result doubles as the keyboard anchor.
        rr = await m.answer(final, reply_markup=main_menu())
        _track_more(uid, "req", rr.message_id)
        # The admin copy drops the geo internals; on expired/failed it
        # carries a one-tap retry button.
        await _broadcast(m.bot, place.name, uid,
                         f"{place.name}: {final.splitlines()[0]}",
                         exclude={uid},
                         reply_markup=_retry_kb(req_id, state))
        if state == "expired" and req_id:
            asyncio.create_task(_remind_admin(m.bot, req_id))
    except CheckinCancelled:
        log.info("checkin: superseded by a newer request")
        try:
            await run_in_thread(device.stop_visit)
        except Exception:
            pass
        await _set_progress("🚫 Заезд отменён новым запросом.")
        r = await m.answer("🚫 Заезд отменён новым запросом.",
                           reply_markup=main_menu())
        _track_more(uid, "req", r.message_id)
    except Exception as e:
        log.exception("checkin failed")
        sp = device.last_spoof
        geo = ""
        if sp:
            geo = (f"\n📍 {sp[0]},{sp[1]} → {sp[2]:.6f},{sp[3]:.6f} "
                   f"(смещение {sp[4]:.0f} м)")
        await _set_progress(f"Failed at {last_step[0]}: {e}{geo}")
        await m.answer(f"Failed at {last_step[0]}: {e}{geo}",
                       reply_markup=main_menu())
        if req_id:
            store.set_request_state(req_id, "failed")
            asyncio.create_task(_remind_admin(m.bot, req_id))
        await _broadcast(m.bot, place.name, uid,
                         f"{place.name}: ошибка — {e}", exclude={uid},
                         reply_markup=_retry_kb(req_id, "failed"))
        try:
            shot = await run_in_thread(device.screenshot)
            ph = await m.answer_photo(
                BufferedInputFile(shot, filename="screen.png"),
                reply_markup=main_menu())
            _track(uid, "checkin", progress.message_id, ph.message_id)
        except Exception:
            _track(uid, "checkin", progress.message_id)


@router.message(F.text == BTN_CHECKIN)
@owner_only
async def menu_checkin(m: types.Message):
    await _drop(m)
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
    await _drop(c.message)
    await offer_services(c.message, c.from_user.id, place)


@router.message(F.text == BTN_PLACES)
@owner_only
async def menu_places(m: types.Message):
    await cmd_places(m)


@router.message(F.text == BTN_ADD_PLACE)
@owner_only
async def menu_add_place(m: types.Message):
    await _drop(m)
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
async def cmd_update(m: types.Message, uid: int | None = None):
    """Self-update: git pull in the repo dir + restart the systemd unit."""
    import os

    uid = uid or m.from_user.id
    await _drop(m)
    # Sweep old transient messages before updating — the boot-time
    # cleanup would only run after the restart.
    for kind in ("update", "menu", "status", "shot", "dump",
                 "settings", "checkin", "req"):
        try:
            await _untrack(m.bot, uid, kind)
        except Exception:
            pass

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
        "Перезапускаю бота… (если он молчит пару минут — проверь journalctl)",
        reply_markup=main_menu())
    _track(uid, "update", m1.message_id, m2.message_id)
    await asyncio.create_subprocess_exec("systemctl", "restart", "asbot")


@router.message(Command("stop"))
@owner_only
async def cmd_stop(m: types.Message):
    await _drop(m)
    for pend in _pending_req.values():
        if pend["owner"] == m.from_user.id and pend["result"] is None:
            pend["result"] = "cancelled"
            pend["event"].set()
    store.close_open_requests()  # the chip must leave the keyboard at once;
    # the waiting coroutine still broadcasts the ⛔ cancellation to admins
    device.cancel()  # abort a check-in running under the lock as well
    try:
        await run_in_thread(device.connect)
        await run_in_thread(device.stop_visit)
        rr = await m.answer("Визит остановлен: геопозиция снята, Allsports закрыт",
                            reply_markup=main_menu())
        _track_more(m.from_user.id, "req", rr.message_id)
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
async def cmd_dump(m: types.Message, uid: int | None = None):
    uid = uid or m.from_user.id
    await _drop(m)
    await _untrack(m.bot, uid, "dump")
    try:
        await run_in_thread(device.connect)
        xml = await run_in_thread(device.dump_ui)
        r = await m.answer_document(
            BufferedInputFile(xml.encode(), filename="hierarchy.xml"),
            caption="UI hierarchy",
        )
        _track(uid, "dump", r.message_id)
    except Exception as e:
        log.exception("dump failed")
        await m.answer(f"Failed: {e}")


@router.message(Command("status"))
@owner_only
async def cmd_status(m: types.Message, uid: int | None = None):
    uid = uid or m.from_user.id
    await _drop(m)
    await _untrack(m.bot, uid, "status")
    req = store.current_request()
    if req:
        svc = f" · {req['service']}" if req["service"] else ""
        req_line = f"Текущий запрос: «{req['venue']}»{svc} — {req['state']}"
    else:
        req_line = "Активных запросов нет."
    try:
        await run_in_thread(device.connect)
        app = await run_in_thread(device.current_app)
        r = await m.answer(f"Device OK\nForeground: {app}\n{req_line}",
                           reply_markup=main_menu())
    except DeviceError as e:
        r = await m.answer(f"Device unreachable: {e}\n{req_line}",
                           reply_markup=main_menu())
    _track(uid, "status", r.message_id)


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
        [InlineKeyboardButton(
            text="✏️ Мой псевдоним",
            callback_data="setname")],
        [InlineKeyboardButton(text="🖥 Статус устройства",
                              callback_data="setstatus")],
        [InlineKeyboardButton(text="🧩 Дамп UI", callback_data="setdump")],
        [InlineKeyboardButton(text="🔄 Обновить бота", callback_data="setupdate")],
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


@router.callback_query(F.data == "setstatus")
async def callback_set_status(c: types.CallbackQuery):
    if c.from_user.id not in config.allowed_users:
        return await c.answer()
    await c.answer()
    await cmd_status(c.message, c.from_user.id)


@router.callback_query(F.data == "setdump")
async def callback_set_dump(c: types.CallbackQuery):
    if c.from_user.id not in config.allowed_users:
        return await c.answer()
    await c.answer()
    await cmd_dump(c.message, c.from_user.id)


@router.callback_query(F.data == "setupdate")
async def callback_set_update(c: types.CallbackQuery):
    if c.from_user.id not in config.allowed_users:
        return await c.answer()
    await c.answer()
    await cmd_update(c.message, c.from_user.id)


@router.message(F.text.startswith("⏳"))
@owner_only
async def menu_current_req(m: types.Message):
    await _drop(m)
    req = store.current_request()
    if not req:
        r = await m.answer("Активных запросов нет.",
                           reply_markup=main_menu())
    else:
        svc = f" · {req['service']}" if req["service"] else ""
        r = await m.answer(
            f"Текущий запрос: «{req['venue']}»{svc} — {req['state']}",
            reply_markup=main_menu())
    _track_more(m.from_user.id, "req", r.message_id)


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


@router.message(F.text == BTN_ADM_RESTART)
async def menu_adm_restart(m: types.Message):
    uid = m.from_user.id
    if not store.is_admin(uid):
        return
    await _drop(m)
    reqs = store.open_requests_for_admin(uid)
    if not reqs:
        return await m.answer("Нет открытых запросов.")
    if len(reqs) == 1:
        return await _admin_restart(m, reqs[0])
    _pending_restart[uid] = reqs
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{r['venue']} ({r['state']})",
                              callback_data=f"restrt:{i}")]
        for i, r in enumerate(reqs)])
    await m.answer("Какой запрос перезапустить?", reply_markup=kb)


@router.callback_query(F.data.startswith("restrt:"))
async def callback_restart_pick(c: types.CallbackQuery):
    try:
        req = _pending_restart[c.from_user.id][
            int(c.data.removeprefix("restrt:"))]
    except (KeyError, IndexError, ValueError):
        return await c.answer("Список устарел.", show_alert=True)
    await c.answer()
    await _drop(c.message)
    await _admin_restart(c.message, req)


async def _admin_restart(m: types.Message, req: dict) -> None:
    place = store.get(req["venue"])
    if not place:
        return await m.answer("Объект удалён из списка.")
    await _drop(m)
    await _broadcast(m.bot, place.name, req["owner"],
                     f"🔁 Администратор {await _display_name(m.bot, m.chat.id)} "
                     f"просит повторить "
                     f"«{place.name}» — запускаю.")
    store.set_request_state(req["id"], "ready")
    await _untrack(m.bot, m.chat.id, "admreq")
    await _run_checkin(_OwnerChan(m.bot, req["owner"]), place,
                       req["service"] or None, req["id"])


class _OwnerChan:
    """Message-like shim routing a check-in's progress to the OWNER's
    chat — used by the admin-side restart where `m` is the admin's own
    message, so answers must not land in the admin's chat."""

    def __init__(self, bot, uid: int):
        self.bot = bot
        self.chat = SimpleNamespace(id=uid)

    async def answer(self, text, **kw):
        return await self.bot.send_message(self.chat.id, text, **kw)

    async def answer_photo(self, photo, **kw):
        return await self.bot.send_photo(self.chat.id, photo, **kw)

@router.message(F.text)
async def free_text(m: types.Message):
    uid = m.from_user.id
    if uid in _awaiting_balance and m.from_user.id in config.allowed_users:
        venue, op = _awaiting_balance.pop(uid)
        minutes = _parse_hours(m.text)
        if minutes is None or (minutes <= 0 and op != "set"):
            _awaiting_balance[uid] = (venue, op)
            return await m.answer(
                "Нужно число в часах — например, 1.5. "
                "Можно и в минутах с суффиксом: 45 мин.")
        text = await _apply_balance(m.bot, uid, venue, op, minutes)
        return await m.answer(text, reply_markup=main_menu())
    if uid in _awaiting_name_for and m.from_user.id in config.allowed_users:
        target = _awaiting_name_for.pop(uid)
        store.set_name(target, m.text.strip()[:64])
        return await m.answer(
            f"Псевдоним сохранён: {m.text.strip()[:64]}",
            reply_markup=main_menu())
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
