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

import logging
import os
import re
from datetime import date, timedelta

from telegram import (InlineKeyboardButton, InlineKeyboardMarkup,
                      KeyboardButton, ReplyKeyboardMarkup,
                      ReplyKeyboardRemove, Update)
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, ConversationHandler, MessageHandler,
                          filters)

from suv.config import load_env

load_env()

from suv import __version__
from suv.climate import STATIONS, season
from suv.crop import CROPS
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

# Пустой список = бот открыт всем (демо-режим). На пилоте сюда вписать
# chat_id Фарруха и наблюдателей: ALLOWED_CHAT_IDS=1724124721,123456
_ALLOWED: set[int] = {
    int(x) for x in
    os.environ.get("ALLOWED_CHAT_IDS", "").replace(";", ",").split(",")
    if x.strip().lstrip("-").isdigit()
}

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

MAIN_MENU = ReplyKeyboardMarkup(
    [[BTN_SUV, BTN_BAJARDIM], [BTN_TEJALDI, BTN_YORDAM]],
    resize_keyboard=True)

HOUR_OPTIONS = (6, 8, 9, 10, 12)
MAX_HOURS = 48.0  # опечатка вида /bajardim 999 не должна попадать в KPI

# Подтверждение полива принимается только против свежей рекомендации:
# отметка против прошлонедельной — шум, а не данные.
REC_MAX_AGE_DAYS = 7

# Bosib qoldirsa ham — ro'yxatdan o'tish savoliga javob bermay, menyu
# tugmasini bossa, vizard shu yerda to'xtab qolmasligi kerak.
_MENU_PATTERN = "|".join(re.escape(b) for b in
                         (BTN_SUV, BTN_BAJARDIM, BTN_TEJALDI, BTN_YORDAM))
MENU_FILTER = filters.Regex(f"^({_MENU_PATTERN})$")


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
            reply_markup=MAIN_MENU)
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
        reply_markup=MAIN_MENU)
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
    rows = _owner_fields(update.effective_chat.id)
    if not rows:
        await update.message.reply_text("Avval /start buyrug'ini yuboring.")
        return

    today = date.today()
    ctx.user_data.setdefault("last_rec_ids", {})
    for row in rows:
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
        rid = LEDGER.log_recommendation(rec, __version__)
        ctx.user_data["last_rec_ids"][fld.field_id] = rid

        pump = None
        if row["pump_kwh_per_hour"] and row["pump_m3_per_hour"]:
            from suv.economics import PumpProfile
            pump = PumpProfile(row["pump_kwh_per_hour"], row["pump_m3_per_hour"],
                               row["pump_cost_per_hour_uzs"] or 0.0,
                               row["pump_lift_m"] or 0.0)

        msg = recommendation_text(rec, "uz", pump=pump)
        warn = salinity_warning(rec.plan[0].salinity if rec.plan else "unknown", "uz")
        if warn:
            msg += "\n\n" + warn
        await update.message.reply_text(msg, reply_markup=MAIN_MENU)


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
    rows = _owner_fields(update.effective_chat.id)
    if not rows:
        await update.message.reply_text("Avval /start buyrug'ini yuboring.")
        return
    parts = []
    for row in rows:
        try:
            s = LEDGER.savings(row["field_id"])
        except KeyError:
            continue
        parts.append(f"{row['name']}\n{savings_text(s, 'uz')}")
    await update.message.reply_text("\n\n".join(parts), reply_markup=MAIN_MENU)


async def yordam(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        f"{BTN_SUV} — bugungi sug'orish tavsiyasi\n"
        f"{BTN_BAJARDIM} — suv berganingizni belgilash\n"
        f"{BTN_TEJALDI} — mavsum davomida tejalgan suv\n\n"
        "Yangi dala qo'shish uchun: /start",
        reply_markup=MAIN_MENU)


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


def _log_one(field_id: str, rid: int, hours: float | None) -> str:
    """Записать подтверждение по ОДНОМУ полю. Часы имеют смысл только
    там, где есть насос; самотёку хватает факта «полил»."""
    row = _field_row(field_id)
    m3 = None
    if hours is not None and row and row["pump_m3_per_hour"]:
        m3 = hours * row["pump_m3_per_hour"]
    LEDGER.log_action(rid, followed=True, actual_day=date.today(),
                      actual_m3=m3, source="farmer",
                      note=f"{hours:g} soat" if hours is not None else None)
    name = row["name"] if row else field_id
    if hours is not None:
        return f"{name}: yozib oldim, {hours:g} soat. Rahmat!"
    return f"{name}: yozib oldim. Rahmat!"


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
    if hours is None and has_pump:
        await update.message.reply_text("Nasos necha soat ishladi?",
                                        reply_markup=_hour_keyboard(fid))
        return
    await update.message.reply_text(_log_one(fid, rid, hours),
                                    reply_markup=MAIN_MENU)


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
    if hours is None and has_pump:
        await query.edit_message_text("Nasos necha soat ishladi?",
                                      reply_markup=_hour_keyboard(fid))
        return
    await query.edit_message_text(_log_one(fid, ids[fid], hours))


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
    await query.edit_message_text(_log_one(fid, ids[fid], hours))


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
    app.add_handler(CallbackQueryHandler(bajardim_field_callback, pattern=r"^bajfld:"))
    app.add_handler(CallbackQueryHandler(bajardim_hours_callback, pattern=r"^bajardim:"))
    app.add_error_handler(on_error)
    if _ALLOWED:
        log.info("allowlist active: %s", sorted(_ALLOWED))
    log.info("SUV AI bot v%s starting", __version__)
    app.run_polling()


if __name__ == "__main__":
    main()
