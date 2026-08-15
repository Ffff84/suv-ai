"""
SUV AI Telegram bot.

Run:
    export TELEGRAM_TOKEN=...      # from @BotFather, takes 2 minutes
    python -m bot.main

Not exercised in CI — the test environment has no outbound network.
The engine underneath it is fully tested; this file is the thin shell.

Conversation design: registration is five questions, not a form. Every
extra field is a farmer who never finishes onboarding — but the planting
date could not be dropped: without it every annual crop starts life at
Kc_ini and the bot under-waters the whole season.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from datetime import date, timedelta

from telegram import (InlineKeyboardButton, InlineKeyboardMarkup,
                      KeyboardButton, ReplyKeyboardMarkup,
                      ReplyKeyboardRemove, Update)
from telegram.constants import ChatAction
from telegram.error import BadRequest
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, ConversationHandler, MessageHandler,
                          filters)

from suv.config import load_env

load_env()

from suv import __version__
from suv.climate import STATIONS, season
from suv.crop import CROPS
from suv.field_status import (LIST_HEADER, Status, assemble, cost_section,
                              field_list_label, overall_status, render_card,
                              water_section, weather_section)
from suv.ledger import Ledger
from suv.messages import recommendation_text, salinity_warning, savings_text
from suv.schedule import Field, recommend, simulate
from suv.soil import SOILS, WaterBalanceState
from suv.weather import fetch_forecast

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
# httpx пишет полный URL каждого запроса — вместе с токеном бота.
# journalctl на VPS или веб-логи Railway не должны содержать токен.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("suv.bot")

CROP, PLANTING, HECTARES, METHOD, LOCATION = range(5)
LEDGER = Ledger(os.environ.get("SUV_DB", "suv.db"))

def _ids_from_env(name: str) -> set[int]:
    return {int(x) for x in
            os.environ.get(name, "").replace(";", ",").split(",")
            if x.strip().lstrip("-").isdigit()}


# Пустой список = бот открыт всем (демо-режим). На пилоте сюда вписать
# chat_id фермера и наблюдателей: ALLOWED_CHAT_IDS=111111111,222222222
_ALLOWED: set[int] = _ids_from_env("ALLOWED_CHAT_IDS")

# Наблюдатель видит /suv и /tejaldi по ВСЕМ полям, но по-русски (язык
# агронома, см. messages.py) и без следа в KPI-журнале: его любопытство
# не должно плодить строки recommendations, по которым потом считается
# дисциплина фермера. Отмечать поливы наблюдатель тоже не может.
_OBSERVERS: set[int] = _ids_from_env("OBSERVER_CHAT_IDS")

CROP_EMOJI = {"cotton": "🌱", "winter_wheat": "🌾", "onion": "🧅", "tomato": "🍅"}
CROP_ORDER = ("cotton", "winter_wheat", "onion", "tomato")


def _crop_label(key: str) -> str:
    return f"{CROP_EMOJI[key]} {CROPS[key].name_uz}"


METHOD_LABELS = {"furrow": "🚜 Egat (furrow)", "sprinkler": "🌧 Yomg'irlatib",
                 "drip": "💧 Tomchilatib"}
LOCATION_LABEL = "📍 Joylashuvni yuborish"

MONTHS_UZ = ("Yanvar", "Fevral", "Mart", "Aprel", "May", "Iyun",
             "Iyul", "Avgust", "Sentabr", "Oktabr", "Noyabr", "Dekabr")

BTN_SUV = "💧 Suv holati"
BTN_BAJARDIM = "✅ Suv berdim"
BTN_TEJALDI = "📊 Tejaldi"
BTN_YORDAM = "❓ Yordam"
BTN_DALA = "🌾 Dala holati"

MAIN_MENU = ReplyKeyboardMarkup(
    [[BTN_SUV, BTN_BAJARDIM], [BTN_TEJALDI, BTN_YORDAM]],
    resize_keyboard=True)

# Экран «Dala holati» обкатывается в закрытом демо: кнопку и хендлеры
# видят только chat_id из FIELD_STATUS_CHAT_IDS. Пусто = фичи ни у кого
# нет, и деплой этой ветки ничего не меняет для пилотного фермера.
_FIELD_STATUS: set[int] = _ids_from_env("FIELD_STATUS_CHAT_IDS")

MAIN_MENU_DALA = ReplyKeyboardMarkup(
    [[BTN_SUV, BTN_BAJARDIM], [BTN_TEJALDI, BTN_YORDAM], [BTN_DALA]],
    resize_keyboard=True)


def _menu(chat_id: int) -> ReplyKeyboardMarkup:
    """Главное меню чата. Демо-участники видят третью строку с «Dala
    holati», остальные — ровно прежнюю клавиатуру."""
    return MAIN_MENU_DALA if chat_id in _FIELD_STATUS else MAIN_MENU

HOUR_OPTIONS = (6, 8, 9, 10, 12)
MAX_HOURS = 48.0  # опечатка вида /bajardim 999 не должна попадать в KPI

# Подтверждение полива принимается только против свежей рекомендации:
# отметка против прошлонедельной — шум, а не данные.
REC_MAX_AGE_DAYS = 7

# Утренний автопуш: фермер читает до выхода в поле.
PUSH_HOUR_TASHKENT = 6
# В спокойные дни бот молчит, если совет уходил недавно: ежедневный
# «поливать не надо» превращается в спам, и его перестают читать.
PUSH_QUIET_DAYS = 3

# Bosib qoldirsa ham — ro'yxatdan o'tish savoliga javob bermay, menyu
# tugmasini bossa, vizard shu yerda to'xtab qolmasligi kerak.
_MENU_PATTERN = "|".join(re.escape(b) for b in
                         (BTN_SUV, BTN_BAJARDIM, BTN_TEJALDI, BTN_YORDAM))
# Кнопка демо-экрана вычитается из вопросов мастера ТОЛЬКО у демо-чатов:
# filters.Chat(пустое множество) не совпадает ни с кем, так что для всех
# остальных регистрация ведёт себя ровно как до этой ветки.
DALA_FILTER = (filters.Regex(f"^{re.escape(BTN_DALA)}$")
               & filters.Chat(_FIELD_STATUS))
MENU_FILTER = filters.Regex(f"^({_MENU_PATTERN})$") | DALA_FILTER


def _authorized(update: Update) -> bool:
    return not _ALLOWED or update.effective_chat.id in _ALLOWED


async def _reject(update: Update) -> None:
    await update.message.reply_text(
        "Bu bot yopiq sinovda. Qatnashish uchun egasiga murojaat qiling.")


def _owner_fields(chat_id: int) -> list:
    import sqlite3
    with sqlite3.connect(LEDGER.path) as c:
        c.row_factory = sqlite3.Row
        return c.execute("SELECT * FROM fields WHERE owner_chat_id=? "
                         "ORDER BY field_id", (chat_id,)).fetchall()


def _all_fields() -> list:
    import sqlite3
    with sqlite3.connect(LEDGER.path) as c:
        c.row_factory = sqlite3.Row
        return c.execute("SELECT * FROM fields ORDER BY field_id").fetchall()


def _fields_for_view(chat_id: int) -> tuple[list, bool]:
    """Поля для показа: свои — как владелец; чужие — только наблюдателю.
    Возвращает (строки, наблюдатель_ли)."""
    rows = _owner_fields(chat_id)
    if rows:
        return rows, False
    if chat_id in _OBSERVERS:
        return _all_fields(), True
    return [], False


# ---------------------------------------------------------------- wizard

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not _authorized(update):
        await _reject(update)
        return ConversationHandler.END

    chat = update.effective_chat.id
    seeded = [r for r in _owner_fields(chat) if r["field_id"] != f"TG-{chat}"]
    if seeded:
        # Поля этого хозяйства заведены агрономом из конфига. Повторная
        # регистрация создала бы третье поле с параметрами по умолчанию
        # (суглинок, 500 м, без насоса) — и /suv слал бы лишний, неверный
        # совет рядом с двумя настоящими.
        names = ", ".join(r["name"] for r in seeded)
        await update.message.reply_text(
            f"Sizning dalalaringiz allaqachon ro'yxatda: {names}.\n"
            f"Tavsiya olish uchun “{BTN_SUV}” tugmasini bosing.",
            reply_markup=_menu(chat))
        return ConversationHandler.END

    kb = [[_crop_label(CROP_ORDER[0]), _crop_label(CROP_ORDER[1])],
          [_crop_label(CROP_ORDER[2]), _crop_label(CROP_ORDER[3])]]
    await update.message.reply_text(
        "Assalomu alaykum!\n\n"
        "Men sizga qachon va qancha sug'orish kerakligini aytaman.\n\n"
        "Dalangizda nima ekilgan?",
        reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True,
                                         resize_keyboard=True))
    return CROP


async def got_crop(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    name = update.message.text.strip()
    key = next((k for k in CROP_ORDER if _crop_label(k) == name), None)
    if key is None:
        await update.message.reply_text("Iltimos, ro'yxatdan tanlang.")
        return CROP
    ctx.user_data["crop"] = key
    kb = [list(MONTHS_UZ[i:i + 3]) for i in range(0, 12, 3)]
    await update.message.reply_text(
        "Qaysi oyda ekkansiz?",
        reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True,
                                         resize_keyboard=True))
    return PLANTING


async def got_planting(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Дата сева — то, чего в мастере не было. Регистрация ставила
    planting_date = сегодня, и хлопок, посеянный в апреле, а
    зарегистрированный в августе, получал Kc начальной стадии — совет
    поливать в разы меньше нужного, весь остаток сезона.

    Точный день фермер помнит редко, месяц — всегда. Ошибка в ±10 дней
    несравнима с ошибкой в четыре месяца.
    """
    name = update.message.text.strip()
    if name not in MONTHS_UZ:
        await update.message.reply_text("Iltimos, oyni ro'yxatdan tanlang.")
        return PLANTING
    month = MONTHS_UZ.index(name) + 1
    today = date.today()
    year = today.year if month <= today.month else today.year - 1
    crop = CROPS[ctx.user_data["crop"]]
    day = crop.typical_sowing[1] if month == crop.typical_sowing[0] else 15
    ctx.user_data["planting"] = date(year, month, day)
    await update.message.reply_text("Dala necha gektar?",
                                    reply_markup=ReplyKeyboardRemove())
    return HECTARES


async def got_hectares(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        ha = float(update.message.text.replace(",", "."))
        if not 0.01 <= ha <= 10_000:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Raqam kiriting. Masalan: 4 yoki 2.5")
        return HECTARES
    ctx.user_data["hectares"] = ha
    kb = [[METHOD_LABELS["furrow"]], [METHOD_LABELS["sprinkler"]],
          [METHOD_LABELS["drip"]]]
    await update.message.reply_text(
        "Qanday sug'orasiz?",
        reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True,
                                         resize_keyboard=True))
    return METHOD


async def got_method(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    t = update.message.text
    ctx.user_data["method"] = ("drip" if "Tomchi" in t
                               else "sprinkler" if "Yomg" in t else "furrow")
    await update.message.reply_text(
        "Oxirgi qadam: dalangiz joylashuvini yuboring.",
        reply_markup=ReplyKeyboardMarkup(
            [[KeyboardButton(LOCATION_LABEL, request_location=True)]],
            one_time_keyboard=True, resize_keyboard=True))
    return LOCATION


async def got_location(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    loc = update.message.location
    chat = update.effective_chat.id
    fid = f"TG-{chat}"

    LEDGER.upsert_field(
        field_id=fid, name="Mening dalam", owner_chat_id=chat,
        hectares=ctx.user_data["hectares"], lat=loc.latitude, lon=loc.longitude,
        elevation_m=500.0, crop_key=ctx.user_data["crop"], soil_key="loam",
        planting_date=ctx.user_data["planting"].isoformat(),
        irrigation_method=ctx.user_data["method"],
        water_table_depth_m=0.0, baseline_m3_per_ha=None,
        baseline_interval_days=30, last_irrigation_date=None)

    await update.message.reply_text(
        "Dala saqlandi.\n\n"
        "Har 3 kunda sizga sug'orish bo'yicha xabar yuboraman.\n"
        f"Hozir tekshirish uchun pastdagi “{BTN_SUV}” tugmasini bosing.",
        reply_markup=_menu(chat))
    return ConversationHandler.END


# ---------------------------------------------------------------- engine glue

def _build_field(row) -> Field:
    return Field(
        field_id=row["field_id"], name=row["name"], hectares=row["hectares"],
        lat=row["lat"], lon=row["lon"], elevation_m=row["elevation_m"],
        crop=CROPS[row["crop_key"]], soil=SOILS[row["soil_key"]],
        planting_date=date.fromisoformat(row["planting_date"]),
        irrigation_method=row["irrigation_method"],
        water_table_depth_m=row["water_table_depth_m"] or 0.0)


def _last_irrigation(field_id: str, seeded: str | None) -> date | None:
    """
    Дата последнего полива: свежайший подтверждённый /bajardim, а если
    фермер ещё ничего не отмечал — дата из конфига поля (заполняется при
    заведении). Так подтверждения фермера сами уточняют следующий расчёт.
    """
    import sqlite3
    with sqlite3.connect(LEDGER.path) as c:
        row = c.execute(
            """SELECT MAX(a.actual_day) d FROM actions a
               JOIN recommendations r ON r.id = a.recommendation_id
               WHERE r.field_id=? AND a.followed=1 AND a.actual_day IS NOT NULL""",
            (field_id,)).fetchone()
    if row and row[0]:
        return date.fromisoformat(row[0])
    return date.fromisoformat(seeded) if seeded else None


def _warm_state(fld: Field, last_irr: date | None,
                today: date) -> tuple[WaterBalanceState, list]:
    """
    Отмотать водный баланс от последнего полива до сегодня и вернуть
    (состояние, прогноз на будущее).

    Раньше бот всегда стартовал с «поле только что полито» — depletion 0.
    Логика отмотки существовала, но только в scripts/run_field.py; бот
    её не использовал и системно откладывал полив.
    """
    gap = (today - last_irr).days if last_irr else 0
    gap = max(0, min(gap, 92))  # 92 — предел архива Open-Meteo

    series = fetch_forecast(fld.lat, fld.lon, days=14, past_days=gap)
    history, future = series[:gap], series[gap:]
    if not future:  # ряд обрезался на дырах в архиве — прогноза нет
        raise ValueError("weather series has no forecast part")

    state = WaterBalanceState(0.0, 0.20)
    if history and last_irr:
        plan = simulate(fld, history, state, last_irr, apply_irrigation=False)
        last = plan[-1]
        zr = (last.taw_mm / fld.soil.available_water_mm_per_m
              if last.taw_mm else 0.20)
        state = WaterBalanceState(last.depletion_mm, zr)
    return state, future


# Поле без даты последнего полива считается «только что политым» — это
# документированный компромисс, но молчать о нём нельзя: оптимистичный
# «полив не требуется» на пустых данных — прямой путь засушить поле.
NO_ANCHOR_UZ = ("⚠️ Oxirgi sug'orish sanasi noma'lum — hisob taxminiy.\n"
                "Sug'organingizdan keyin «✅ Suv berdim» tugmasini bosing, "
                "hisob aniqlashadi.")
NO_ANCHOR_RU = ("⚠️ Дата последнего полива неизвестна — расчёт приблизительный "
                "и может недооценивать потребность в воде.")


def _compute_rec(row, today: date):
    """Полный расчёт по одному полю: спутник, warm-start, рекомендация.

    Общий путь для кнопки «Suv holati» и утреннего автопуша — совет
    обязан быть одинаковым, каким бы способом фермер его ни получил.
    Возвращает (rec, pump, anchored): anchored=False — расчёту не от
    чего отталкиваться, полю приписана полная влагоёмкость.
    """
    fld = _build_field(row)

    from suv.enrich import attach_ndvi
    log.info("%s: %s", fld.field_id, attach_ndvi(fld))

    last_irr = _last_irrigation(fld.field_id, row["last_irrigation_date"])
    try:
        state, forecast = _warm_state(fld, last_irr, today)
    except Exception as exc:  # noqa: BLE001
        log.warning("weather feed failed for %s: %s", fld.field_id, exc)
        state = WaterBalanceState(0.0, 0.20)
        forecast = season(STATIONS["samarkand"], today, 14)

    rec = recommend(fld, forecast, state, today)

    pump = None
    if row["pump_kwh_per_hour"] and row["pump_m3_per_hour"]:
        from suv.economics import PumpProfile
        pump = PumpProfile(row["pump_kwh_per_hour"], row["pump_m3_per_hour"],
                           row["pump_cost_per_hour_uzs"] or 0.0,
                           row["pump_lift_m"] or 0.0)
    return rec, pump, last_irr is not None


def _rec_message(rec, pump, lang: str, anchored: bool = True) -> str:
    msg = recommendation_text(rec, lang, pump=pump)
    warn = salinity_warning(rec.plan[0].salinity if rec.plan else "unknown", lang)
    if warn:
        msg += "\n\n" + warn
    if not anchored:
        msg += "\n\n" + (NO_ANCHOR_UZ if lang == "uz" else NO_ANCHOR_RU)
    return msg


async def suv(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Главная команда. Отвечает по КАЖДОМУ полю владельца.

    Раньше здесь стоял fetchone(): у фермера с двумя полями бот молча
    отвечал по одному, произвольному. Для Фарруха это виноградник или сад
    через раз — и он бы никогда не понял, почему совет не сходится.
    """
    if not _authorized(update):
        await _reject(update)
        return
    rows, observer = _fields_for_view(update.effective_chat.id)
    if not rows:
        await update.message.reply_text("Avval /start buyrug'ini yuboring.")
        return
    lang = "ru" if observer else "uz"
    if observer:
        await update.message.reply_text(
            "👁 Режим наблюдателя: показываю все поля, в журнал не пишу.")

    today = date.today()
    ctx.user_data.setdefault("last_rec_ids", {})
    for row in rows:
        rec, pump, anchored = _compute_rec(row, today)
        if not observer:
            rid = LEDGER.log_recommendation(rec, __version__)
            ctx.user_data["last_rec_ids"][rec.field.field_id] = rid
        await update.message.reply_text(
            _rec_message(rec, pump, lang, anchored=anchored),
            reply_markup=_menu(update.effective_chat.id))


async def tejaldi(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /tejaldi — how much has been saved. The KPI, farmer-facing.

    Ищет по владельцу, как /suv, — раньше тут был захардкожен id вида
    TG-{chat}, и для полей, заведённых агрономом (FAR-*), команда
    отвечала «сначала /start», хотя поля есть.
    """
    if not _authorized(update):
        await _reject(update)
        return
    rows, observer = _fields_for_view(update.effective_chat.id)
    if not rows:
        await update.message.reply_text("Avval /start buyrug'ini yuboring.")
        return
    lang = "ru" if observer else "uz"
    parts = []
    for row in rows:
        try:
            s = LEDGER.savings(row["field_id"])
        except KeyError:
            continue
        parts.append(f"{row['name']}\n{savings_text(s, lang)}")
    await update.message.reply_text("\n\n".join(parts),
                                    reply_markup=_menu(update.effective_chat.id))


async def yordam(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat.id
    dala_line = (f"{BTN_DALA} — dala holati bir ekranda\n"
                 if chat in _FIELD_STATUS else "")
    await update.message.reply_text(
        f"{BTN_SUV} — bugungi sug'orish tavsiyasi\n"
        f"{BTN_BAJARDIM} — suv berganingizni belgilash\n"
        f"{BTN_TEJALDI} — mavsum davomida tejalgan suv\n"
        f"{dala_line}\n"
        "Yangi dala qo'shish uchun: /start",
        reply_markup=_menu(chat))


# ---------------------------------------------------------------- bajardim

def _recent_rec_ids(chat_id: int) -> dict[str, int]:
    """
    Свежие рекомендации по полям владельца — из базы, не из памяти.

    ctx.user_data живёт до первого рестарта процесса; фермер, нажавший
    «Suv berdim» после передеплоя, не должен слышать «сначала /suv»,
    если рекомендация утром уходила.
    """
    import sqlite3
    cutoff = (date.today() - timedelta(days=REC_MAX_AGE_DAYS)).isoformat()
    with sqlite3.connect(LEDGER.path) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            """SELECT r.field_id, MAX(r.id) rid FROM recommendations r
               JOIN fields f ON f.field_id = r.field_id
               WHERE f.owner_chat_id=? AND r.generated_on >= ?
               GROUP BY r.field_id""", (chat_id, cutoff)).fetchall()
    return {r["field_id"]: r["rid"] for r in rows}


def _rec_ids(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> dict[str, int]:
    ids = ctx.user_data.get("last_rec_ids") or {}
    if not ids:
        ids = _recent_rec_ids(update.effective_chat.id)
        if ids:
            ctx.user_data["last_rec_ids"] = ids
    return ids


def _field_row(field_id: str):
    import sqlite3
    with sqlite3.connect(LEDGER.path) as c:
        c.row_factory = sqlite3.Row
        return c.execute("SELECT * FROM fields WHERE field_id=?",
                         (field_id,)).fetchone()


def _log_one(ctx: ContextTypes.DEFAULT_TYPE, field_id: str, rid: int,
             hours: float | None, days_ago: int = 0) -> str:
    """Записать подтверждение по ОДНОМУ полю. Часы имеют смысл только
    там, где есть насос; самотёку хватает факта «полил».

    days_ago — фермер подтверждает вчерашний полив: якорь водного
    баланса должен встать на день полива, а не на день нажатия.

    ctx нужен, чтобы сбросить кэш экрана «Dala holati»: иначе карточка,
    открытая до отметки, ещё десять минут советует полить поле, которое
    фермер только что полил, — и сама себе противоречит строкой
    «Oxirgi: bugun».
    """
    ctx.user_data.get("fs_cache", {}).pop(field_id, None)
    row = _field_row(field_id)
    m3 = None
    if hours is not None and row and row["pump_m3_per_hour"]:
        m3 = hours * row["pump_m3_per_hour"]
    actual = date.today() - timedelta(days=days_ago)
    LEDGER.log_action(rid, followed=True, actual_day=actual,
                      actual_m3=m3, source="farmer",
                      note=f"{hours:g} soat" if hours is not None else None)
    name = row["name"] if row else field_id
    when = " (kecha)" if days_ago else ""
    if hours is not None:
        return f"{name}: yozib oldim{when}, {hours:g} soat. Rahmat!"
    return f"{name}: yozib oldim{when}. Rahmat!"


def _day_keyboard(field_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Bugun", callback_data=f"bajday:{field_id}:0"),
        InlineKeyboardButton("Kecha", callback_data=f"bajday:{field_id}:1"),
    ]])


def _hour_keyboard(field_id: str) -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton(f"{h} soat",
                                callback_data=f"bajardim:{field_id}:{h}")
           for h in HOUR_OPTIONS]
    return InlineKeyboardMarkup([row[:3], row[3:]])


def _field_keyboard(ids: dict[str, int]) -> InlineKeyboardMarkup:
    rows = []
    for fid in ids:
        r = _field_row(fid)
        rows.append([InlineKeyboardButton(r["name"] if r else fid,
                                          callback_data=f"bajfld:{fid}")])
    return InlineKeyboardMarkup(rows)


def _parse_hours(text: str) -> float | None:
    """Часы из ввода фермера. None = не число или вне здравого смысла."""
    try:
        hours = float(text.replace(",", "."))
    except ValueError:
        return None
    if not 0.5 <= hours <= MAX_HOURS:
        return None
    return hours


async def bajardim(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /bajardim — «я полил». Замыкает петлю, из которой берётся KPI.

    Можно указать часы сразу: /bajardim 9. Без аргумента — кнопки.
    При нескольких полях сначала спрашивается, КАКОЕ поле полито:
    раньше одна отметка с одними часами уходила во все поля сразу,
    включая самотёчный виноградник, где часы насоса бессмысленны.
    """
    if not _authorized(update):
        await _reject(update)
        return
    chat = update.effective_chat.id
    if chat in _OBSERVERS and not _owner_fields(chat):
        await update.message.reply_text(
            "Наблюдатель не отмечает поливы — это делает хозяин поля.")
        return
    ids = _rec_ids(update, ctx)
    if not ids:
        await update.message.reply_text("Avval /suv buyrug'ini yuboring.")
        return

    hours = None
    if ctx.args:
        hours = _parse_hours(ctx.args[0])
        if hours is None:
            await update.message.reply_text(
                f"Soatni 0.5 dan {MAX_HOURS:g} gacha raqam bilan yozing. "
                f"Masalan: /bajardim 9")
            return

    if len(ids) > 1:
        ctx.user_data["bajardim_hours"] = hours
        await update.message.reply_text("Qaysi dalani sug'ordingiz?",
                                        reply_markup=_field_keyboard(ids))
        return

    fid, rid = next(iter(ids.items()))
    row = _field_row(fid)
    has_pump = bool(row and row["pump_m3_per_hour"])
    if has_pump:
        if hours is None:
            await update.message.reply_text("Nasos necha soat ishladi?",
                                            reply_markup=_hour_keyboard(fid))
            return
        await update.message.reply_text(_log_one(ctx, fid, rid, hours),
                                        reply_markup=_menu(chat))
        return
    # Самотёк: часов нет, но день полива важен для якоря баланса —
    # фермер нередко отмечает вчерашний залив.
    await update.message.reply_text("Qachon sug'ordingiz?",
                                    reply_markup=_day_keyboard(fid))


async def bajardim_field_callback(update: Update,
                                  ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    ids = _rec_ids(update, ctx)
    fid = query.data.split(":", 1)[1]
    if fid not in ids:
        await query.edit_message_text("Avval /suv buyrug'ini yuboring.")
        return
    hours = ctx.user_data.pop("bajardim_hours", None)
    row = _field_row(fid)
    has_pump = bool(row and row["pump_m3_per_hour"])
    if has_pump:
        if hours is None:
            await query.edit_message_text("Nasos necha soat ishladi?",
                                          reply_markup=_hour_keyboard(fid))
        else:
            await query.edit_message_text(_log_one(ctx, fid, ids[fid], hours))
        return
    await query.edit_message_text("Qachon sug'ordingiz?",
                                  reply_markup=_day_keyboard(fid))


async def bajardim_day_callback(update: Update,
                                ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Bugun/Kecha для самотёчных полей: якорь встаёт на день полива."""
    query = update.callback_query
    await query.answer()
    ids = _rec_ids(update, ctx)
    try:
        _, fid, off = query.data.split(":", 2)
        days_ago = int(off)
    except ValueError:
        await query.edit_message_text(
            "Tushunarsiz javob. /bajardim ni qayta bosing.")
        return
    if fid not in ids or days_ago not in (0, 1):
        await query.edit_message_text("Avval /suv buyrug'ini yuboring.")
        return
    await query.edit_message_text(_log_one(ctx, fid, ids[fid], None, days_ago))


async def bajardim_hours_callback(update: Update,
                                  ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    ids = _rec_ids(update, ctx)
    try:
        _, fid, raw = query.data.split(":", 2)
        hours = float(raw)
    except ValueError:
        # callback_data приходит от клиента и подделывается модифицированным
        # приложением; кривые данные — не повод ронять обработчик.
        await query.edit_message_text("Tushunarsiz javob. /bajardim ni qayta bosing.")
        return
    if fid not in ids or not 0.5 <= hours <= MAX_HOURS:
        await query.edit_message_text("Avval /suv buyrug'ini yuboring.")
        return
    await query.edit_message_text(_log_one(ctx, fid, ids[fid], hours))


# ---------------------------------------------------------------- dala holati
#
# Экран «Состояние поля» (ТЗ FieldStatus, день 1): реестр секций живёт
# в suv/field_status.py, здесь только сборка данных и навигация.
# Вся навигация правит ОДНО сообщение (edit_message_text): одно нажатие
# кнопки = одно сообщение в истории, утренняя рекомендация не тонет.

# Карточка тянет спутник и погоду; повторный вход из списка в поле не
# должен ждать сеть заново. Устареть за 10 минут там нечему: снимки
# дневные, прогноз обновляется реже.
FS_CACHE_TTL_S = 600.0


def _fs_data(row, ctx):
    """(rec, pump, anchored, forecast) по полю, с TTL-кэшем на чат.

    Прогноз для секции погоды берётся отдельным быстрым запросом, а не
    протаскивается через _compute_rec: общий путь /suv и автопуша
    трогать ради новой секции нельзя.
    """
    cache = ctx.user_data.setdefault("fs_cache", {})
    hit = cache.get(row["field_id"])
    if hit and time.monotonic() - hit[0] < FS_CACHE_TTL_S:
        return hit[1]
    rec, pump, anchored = _compute_rec(row, date.today())
    try:
        forecast = fetch_forecast(row["lat"], row["lon"], days=3)
    except Exception as exc:  # noqa: BLE001 — погода не роняет карточку
        log.warning("dala: прогноз для %s не пришёл: %s",
                    row["field_id"], exc)
        forecast = []
    data = (rec, pump, anchored, forecast)
    cache[row["field_id"]] = (time.monotonic(), data)
    return data


def _fs_sections(row, ctx, lang: str) -> list:
    rec, pump, _anchored, forecast = _fs_data(row, ctx)
    last_irr = _last_irrigation(row["field_id"], row["last_irrigation_date"])
    try:
        season_m3 = LEDGER.savings(row["field_id"]).metered_m3
    except KeyError:
        season_m3 = 0.0
    return assemble(
        water_section(rec, last_irr, date.today(), pump, lang),
        weather_section(forecast, lang),
        cost_section(season_m3, pump, lang),
    )


def _crop_name(row, lang: str) -> str:
    crop = CROPS.get(row["crop_key"])
    if crop is None:
        return row["crop_key"]
    return crop.name_uz if lang == "uz" else crop.name_ru


def _fs_card(row, chat: int, ctx, lang: str,
             many: bool) -> tuple[str, InlineKeyboardMarkup]:
    sections = _fs_sections(row, ctx, lang)
    text = render_card(row["name"], row["hectares"], _crop_name(row, lang),
                       sections, lang)
    owner = row["owner_chat_id"] == chat
    if owner:
        # В KPI-журнал идут только заходы хозяина поля: наблюдатель,
        # листающий чужие карточки перед питчем, не должен изображать
        # возвраты фермера — та же граница, что и в /suv.
        LEDGER.log_field_status_view(row["field_id"], chat)

    fid = row["field_id"]
    kb: list[list[InlineKeyboardButton]] = []
    if owner:
        label = "🚿 Bugun sug'ordim" if lang == "uz" else "🚿 Полил сегодня"
        kb.append([InlineKeyboardButton(label, callback_data=f"fs:baj:{fid}")])
    nav = [InlineKeyboardButton("🔄 Yangilash" if lang == "uz" else "🔄 Обновить",
                                callback_data=f"fs:re:{fid}")]
    if many:
        nav.append(InlineKeyboardButton(
            "⬅️ Orqaga" if lang == "uz" else "⬅️ Назад",
            callback_data="fs:list"))
    kb.append(nav)
    return text, InlineKeyboardMarkup(kb)


def _fs_list(rows, ctx, lang: str) -> tuple[str, InlineKeyboardMarkup]:
    """Список полей: статус считается по тем же секциям, что и карточка,
    поэтому эмодзи в списке не может разойтись с карточкой внутри."""
    kb = []
    for row in rows:
        try:
            status = overall_status(_fs_sections(row, ctx, lang))
        except Exception as exc:  # noqa: BLE001 — одно упавшее поле
            log.warning("dala: статус %s не посчитался: %s",  # не прячет список
                        row["field_id"], exc)
            status = Status.NO_DATA
        kb.append([InlineKeyboardButton(
            field_list_label(row["name"], row["hectares"],
                             _crop_name(row, lang), status, lang),
            callback_data=f"fs:card:{row['field_id']}")])
    return LIST_HEADER[lang], InlineKeyboardMarkup(kb)


def _fs_screen(rows, chat: int, ctx, lang: str):
    """Первый экран: одно поле — сразу карточка, несколько — список.

    Синхронная и тяжёлая (спутник, погода, sqlite) — вызывается через
    asyncio.to_thread, иначе сетевые запросы держат весь event loop и
    вместе с ним пилотного фермера.
    """
    if len(rows) == 1:
        return _fs_card(rows[0], chat, ctx, lang, many=False)
    return _fs_list(rows, ctx, lang)


# Кнопку «Yangilash» жмут подряд; без паузы каждое нажатие — новый поход
# в Copernicus и Open-Meteo. Данные всё равно дневные.
FS_REFRESH_COOLDOWN_S = 30.0

DALA_CLOSED_UZ = ("Bu funksiya hozircha yopiq sinovda.\n"
                  "Sug'orish tavsiyasi uchun “💧 Suv holati” tugmasini bosing.")
DALA_CLOSED_RU = ("Эта функция пока в закрытом тестировании.\n"
                  "Рекомендация по поливу — кнопка «💧 Suv holati».")


async def dala_holati(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Вход в экран. Хендлеры уже отфильтрованы по DALA_FILTER, но кнопка
    залипает в клавиатуре: чат, выведенный из демо, обязан получить
    внятный ответ, а не тишину («тишина читается как бот умер»)."""
    if not _authorized(update):
        await _reject(update)
        return
    chat = update.effective_chat.id
    if chat not in _FIELD_STATUS:
        await update.message.reply_text(
            DALA_CLOSED_RU if chat in _OBSERVERS else DALA_CLOSED_UZ,
            reply_markup=_menu(chat))
        return
    rows, observer = _fields_for_view(chat)
    if not rows:
        await update.message.reply_text("Avval /start buyrug'ini yuboring.")
        return
    lang = "ru" if observer else "uz"
    await ctx.bot.send_chat_action(chat, ChatAction.TYPING)
    text, kb = await asyncio.to_thread(_fs_screen, rows, chat, ctx, lang)
    await update.message.reply_text(text, reply_markup=kb)


async def _fs_edit(query, text: str, kb: InlineKeyboardMarkup) -> None:
    """edit_message_text, переживающий «Message is not modified»: карточка
    без новых данных законно совпадает со старой, это не ошибка.

    Тост про «изменений нет» здесь не шлётся: колбэк уже отвечен до
    пересчёта, а второй ответ на тот же query_id Telegram не покажет.
    """
    try:
        await query.edit_message_text(text, reply_markup=kb)
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise
        log.info("dala: карточка не изменилась, правка не нужна")


async def fs_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat = update.effective_chat.id
    if not _authorized(update) or chat not in _FIELD_STATUS:
        await query.answer()
        return

    rows, observer = _fields_for_view(chat)
    by_id = {r["field_id"]: r for r in rows}
    lang = "ru" if observer else "uz"
    many = len(rows) > 1

    parts = query.data.split(":", 2)  # fs:list | fs:card:<id> | fs:re:<id> | fs:baj:<id>
    verb = parts[1] if len(parts) > 1 else ""
    fid = parts[2] if len(parts) > 2 else None
    row = by_id.get(fid)

    if verb == "list" and many:
        await query.answer()
        text, kb = await asyncio.to_thread(_fs_list, rows, ctx, lang)
        await _fs_edit(query, text, kb)
        return

    if verb in ("card", "re") and row is not None:
        if verb == "re":
            # Пауза считается по полю: у фермера с двумя полями второе
            # обновление подряд — законное, а не «жму на кнопку зря».
            seen = ctx.user_data.setdefault("fs_refresh_at", {})
            if time.monotonic() - seen.get(fid, 0.0) < FS_REFRESH_COOLDOWN_S:
                await query.answer("Hozirgina yangilandi" if lang == "uz"
                                   else "Только что обновлено")
                return
            seen[fid] = time.monotonic()
            ctx.user_data.get("fs_cache", {}).pop(fid, None)
            await query.answer("Yangilanmoqda…" if lang == "uz"
                               else "Обновляю…")
        else:
            await query.answer()
        await ctx.bot.send_chat_action(chat, ChatAction.TYPING)
        text, kb = await asyncio.to_thread(_fs_card, row, chat, ctx, lang, many)
        await _fs_edit(query, text, kb)
        return

    if verb == "baj" and row is not None:
        # Отметка полива из карточки — тот же путь, что /bajardim:
        # дальше работают существующие клавиатуры и колбэки часов/дня.
        if row["owner_chat_id"] != chat:
            await query.answer("Полив отмечает хозяин поля.", show_alert=True)
            return
        ids = _rec_ids(update, ctx)
        if fid not in ids:
            await query.answer("Avval /suv buyrug'ini yuboring.",
                               show_alert=True)
            return
        await query.answer()
        if row["pump_m3_per_hour"]:
            await query.edit_message_text("Nasos necha soat ishladi?",
                                          reply_markup=_hour_keyboard(fid))
        else:
            await query.edit_message_text("Qachon sug'ordingiz?",
                                          reply_markup=_day_keyboard(fid))
        return

    # Подделанный или устаревший callback — молча гасим «часики».
    await query.answer()


# ---------------------------------------------------------------- автопуш

def push_due(urgent: bool, last_rec: date | None, today: date) -> bool:
    """
    Слать ли фермеру утреннюю рекомендацию.

    Срочное («поливать сегодня или завтра») уходит всегда — ради этого
    автопуш и существует: поле не должно сохнуть из-за того, что фермер
    забыл нажать кнопку. Спокойное — не чаще, чем раз в PUSH_QUIET_DAYS.
    """
    if urgent:
        return True
    if last_rec is None:
        return True
    return (today - last_rec).days >= PUSH_QUIET_DAYS


def _last_rec_date(chat_id: int) -> date | None:
    """Когда владелец в последний раз получал рекомендацию (кнопкой или
    пушем) — по журналу, чтобы пережить рестарты."""
    import sqlite3
    with sqlite3.connect(LEDGER.path) as c:
        row = c.execute(
            """SELECT MAX(r.generated_on) FROM recommendations r
               JOIN fields f ON f.field_id = r.field_id
               WHERE f.owner_chat_id=?""", (chat_id,)).fetchone()
    return date.fromisoformat(row[0]) if row and row[0] else None


async def daily_push(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Утренний обход всех владельцев полей."""
    today = date.today()
    import sqlite3
    with sqlite3.connect(LEDGER.path) as c:
        owners = [r[0] for r in c.execute(
            "SELECT DISTINCT owner_chat_id FROM fields "
            "WHERE owner_chat_id IS NOT NULL")]

    for chat in owners:
        if _ALLOWED and chat not in _ALLOWED:
            continue  # поле в базе есть, а доступа у владельца больше нет
        rows = _owner_fields(chat)
        if not rows:
            continue

        recs = []
        for row in rows:
            try:
                recs.append(_compute_rec(row, today))
            except Exception as exc:  # noqa: BLE001
                log.warning("push: расчёт %s не удался: %s",
                            row["field_id"], exc)
        if not recs:
            continue

        urgent = any(rec.action_day is not None and rec.days_until <= 1
                     for rec, _p, _a in recs)
        if not push_due(urgent, _last_rec_date(chat), today):
            log.info("push: %s — спокойный день, молчим", chat)
            continue

        ud = ctx.application.user_data[chat]
        ud.setdefault("last_rec_ids", {})
        for rec, pump, anchored in recs:
            try:
                await ctx.bot.send_message(
                    chat, _rec_message(rec, pump, "uz", anchored=anchored),
                    reply_markup=_menu(chat))
            except Exception as exc:  # noqa: BLE001
                log.warning("push: отправка %s не удалась: %s", chat, exc)
                continue
            # В журнал — только доставленное: недоставленный совет не
            # должен ни считаться «мы сказали», ни глушить завтрашний пуш.
            rid = LEDGER.log_recommendation(rec, __version__)
            ud["last_rec_ids"][rec.field.field_id] = rid
        log.info("push: %s — отправлено (%d полей, срочно=%s)",
                 chat, len(recs), urgent)


# ---------------------------------------------------------------- wiring

async def _escape_to_suv(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await suv(update, ctx)
    return ConversationHandler.END


async def _escape_to_tejaldi(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await tejaldi(update, ctx)
    return ConversationHandler.END


async def _escape_to_bajardim(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await bajardim(update, ctx)
    return ConversationHandler.END


async def _escape_to_yordam(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await yordam(update, ctx)
    return ConversationHandler.END


async def _escape_to_dala(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await dala_holati(update, ctx)
    return ConversationHandler.END


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Фермер не должен получать тишину: тишина читается как «бот умер»."""
    log.exception("handler failed", exc_info=ctx.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "Kechirasiz, xatolik yuz berdi. Birozdan keyin qayta urinib ko'ring.")
        except Exception:  # noqa: BLE001 — сеть могла упасть целиком
            pass


def main() -> None:
    token = os.environ["TELEGRAM_TOKEN"]
    app = Application.builder().token(token).build()
    # Ro'yxatdan o'tish savollariga JAVOB bo'lishi mumkin bo'lgan matnni
    # menyu tugmalaridan ajratamiz — bo'lmasa, foydalanuvchi savolga
    # javob bermay tugma bossa, vizard uni cheksiz ushlab qoladi.
    not_menu = filters.TEXT & ~filters.COMMAND & ~MENU_FILTER
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CROP: [MessageHandler(not_menu, got_crop)],
            PLANTING: [MessageHandler(not_menu, got_planting)],
            HECTARES: [MessageHandler(not_menu, got_hectares)],
            METHOD: [MessageHandler(not_menu, got_method)],
            LOCATION: [MessageHandler(filters.LOCATION, got_location)],
        },
        fallbacks=[
            CommandHandler("start", start),
            MessageHandler(filters.Regex(f"^{re.escape(BTN_SUV)}$"), _escape_to_suv),
            MessageHandler(filters.Regex(f"^{re.escape(BTN_TEJALDI)}$"), _escape_to_tejaldi),
            MessageHandler(filters.Regex(f"^{re.escape(BTN_BAJARDIM)}$"), _escape_to_bajardim),
            MessageHandler(filters.Regex(f"^{re.escape(BTN_YORDAM)}$"), _escape_to_yordam),
            # Только демо-чаты: у остальных этот текст остаётся обычным
            # ответом на вопрос мастера, как и до появления экрана.
            MessageHandler(DALA_FILTER, _escape_to_dala),
        ],
    ))
    app.add_handler(CommandHandler("suv", suv))
    app.add_handler(CommandHandler("tejaldi", tejaldi))
    app.add_handler(CommandHandler("bajardim", bajardim))
    app.add_handler(CommandHandler("yordam", yordam))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_SUV)}$"), suv))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_TEJALDI)}$"), tejaldi))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_BAJARDIM)}$"), bajardim))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_YORDAM)}$"), yordam))
    # Эти два — БЕЗ фильтра по чату: кнопка залипает в клавиатуре, и чат,
    # выведенный из демо, должен получить внятный отказ, а не тишину.
    # Гейт по чату критичен только в fallback мастера выше, где чужой
    # текст обрывал бы регистрацию.
    app.add_handler(CommandHandler("dala", dala_holati))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_DALA)}$"), dala_holati))
    app.add_handler(CallbackQueryHandler(bajardim_field_callback, pattern=r"^bajfld:"))
    app.add_handler(CallbackQueryHandler(bajardim_hours_callback, pattern=r"^bajardim:"))
    app.add_handler(CallbackQueryHandler(bajardim_day_callback, pattern=r"^bajday:"))
    app.add_handler(CallbackQueryHandler(fs_callback, pattern=r"^fs:"))
    app.add_error_handler(on_error)
    if _ALLOWED:
        log.info("allowlist active: %s", sorted(_ALLOWED))
    if _FIELD_STATUS:
        log.info("Dala holati demo: %s", sorted(_FIELD_STATUS))

    if app.job_queue:
        import datetime as _dt
        from zoneinfo import ZoneInfo
        app.job_queue.run_daily(
            daily_push,
            time=_dt.time(PUSH_HOUR_TASHKENT, 0,
                          tzinfo=ZoneInfo("Asia/Tashkent")))
        log.info("автопуш включён: ежедневно %02d:00 Asia/Tashkent",
                 PUSH_HOUR_TASHKENT)
    else:
        # Без extra job-queue бот обязан работать, но обещание «сам
        # напишу» в этом случае ложь — говорим об этом в лог явно.
        log.warning("JobQueue недоступен (нужен python-telegram-bot"
                    "[job-queue]) — автопуш ВЫКЛЮЧЕН")

    log.info("SUV AI bot v%s starting", __version__)
    app.run_polling()


if __name__ == "__main__":
    main()
