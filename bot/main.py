"""
SUV AI Telegram bot.

Run:
    export TELEGRAM_TOKEN=...      # from @BotFather, takes 2 minutes
    python -m bot.main

Not exercised in CI — the test environment has no outbound network.
The engine underneath it is fully tested; this file is the thin shell.

Conversation design: registration is four questions, not a form. Every
extra field is a farmer who never finishes onboarding.
"""

from __future__ import annotations

import logging
import os
from datetime import date

from telegram import KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.ext import (Application, CommandHandler, ContextTypes,
                          ConversationHandler, MessageHandler, filters)

from suv.config import load_env

load_env()

from suv import __version__
from suv.climate import STATIONS, season
from suv.crop import CROPS
from suv.ledger import Ledger
from suv.messages import recommendation_text, salinity_warning, savings_text
from suv.schedule import Field, recommend
from suv.soil import SOILS, WaterBalanceState
from suv.weather import fetch_forecast

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("suv.bot")

CROP, HECTARES, METHOD, LOCATION = range(4)
LEDGER = Ledger(os.environ.get("SUV_DB", "suv.db"))


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    kb = [[CROPS[k].name_uz] for k in ("cotton", "winter_wheat", "onion", "tomato")]
    await update.message.reply_text(
        "Assalomu alaykum!\n\n"
        "Men sizga qachon va qancha sug'orish kerakligini aytaman.\n\n"
        "Dalangizda nima ekilgan?",
        reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True,
                                         resize_keyboard=True))
    return CROP


async def got_crop(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    name = update.message.text.strip()
    key = next((k for k, c in CROPS.items() if c.name_uz == name), None)
    if key is None:
        await update.message.reply_text("Iltimos, ro'yxatdan tanlang.")
        return CROP
    ctx.user_data["crop"] = key
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
    kb = [["Egat (furrow)"], ["Yomg'irlatib"], ["Tomchilatib"]]
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
            [[KeyboardButton("Joylashuvni yuborish", request_location=True)]],
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
        planting_date=date.today().isoformat(),
        irrigation_method=ctx.user_data["method"],
        water_table_depth_m=0.0, baseline_m3_per_ha=None,
        baseline_interval_days=30)

    await update.message.reply_text(
        "Dala saqlandi.\n\n"
        "Har 3 kunda sizga sug'orish bo'yicha xabar yuboraman.\n"
        "Hozir tekshirish uchun: /suv",
        reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


def _build_field(row) -> Field:
    return Field(
        field_id=row["field_id"], name=row["name"], hectares=row["hectares"],
        lat=row["lat"], lon=row["lon"], elevation_m=row["elevation_m"],
        crop=CROPS[row["crop_key"]], soil=SOILS[row["soil_key"]],
        planting_date=date.fromisoformat(row["planting_date"]),
        irrigation_method=row["irrigation_method"],
        water_table_depth_m=row["water_table_depth_m"] or 0.0)


async def suv(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Главная команда. Отвечает по КАЖДОМУ полю владельца.

    Раньше здесь стоял fetchone(): у фермера с двумя полями бот молча
    отвечал по одному, произвольному. Для Фарруха это виноградник или сад
    через раз — и он бы никогда не понял, почему совет не сходится.
    """
    import sqlite3
    with sqlite3.connect(LEDGER.path) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute("SELECT * FROM fields WHERE owner_chat_id=? "
                         "ORDER BY field_id",
                         (update.effective_chat.id,)).fetchall()
    if not rows:
        await update.message.reply_text("Avval /start buyrug'ini yuboring.")
        return

    ctx.user_data.setdefault("last_rec_ids", {})
    for row in rows:
        fld = _build_field(row)

        from suv.enrich import attach_ndvi
        log.info("%s: %s", fld.field_id, attach_ndvi(fld))

        try:
            forecast = fetch_forecast(fld.lat, fld.lon, days=14)
        except Exception as exc:  # noqa: BLE001
            log.warning("weather feed failed for %s: %s", fld.field_id, exc)
            forecast = season(STATIONS["samarkand"], date.today(), 14)

        rec = recommend(fld, forecast, WaterBalanceState(0.0, 0.20), date.today())
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
        await update.message.reply_text(msg)


async def tejaldi(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/tejaldi — how much has been saved. The KPI, farmer-facing."""
    fid = f"TG-{update.effective_chat.id}"
    try:
        await update.message.reply_text(savings_text(LEDGER.savings(fid), "uz"))
    except KeyError:
        await update.message.reply_text("Avval /start buyrug'ini yuboring.")


async def bajardim(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /bajardim — «я полил». Замыкает петлю, из которой берётся KPI.

    Можно указать часы работы насоса: /bajardim 9
    Часы — единица, которой фермер реально управляет, и из них считается
    и объём, и счёт за электричество.
    """
    ids = ctx.user_data.get("last_rec_ids") or {}
    if not ids:
        await update.message.reply_text("Avval /suv buyrug'ini yuboring.")
        return

    hours = None
    if ctx.args:
        try:
            hours = float(ctx.args[0].replace(",", "."))
        except ValueError:
            await update.message.reply_text("Soatni raqam bilan yozing: /bajardim 9")
            return

    import sqlite3
    for field_id, rid in ids.items():
        m3 = None
        if hours is not None:
            with sqlite3.connect(LEDGER.path) as c:
                c.row_factory = sqlite3.Row
                r = c.execute("SELECT pump_m3_per_hour FROM fields WHERE field_id=?",
                              (field_id,)).fetchone()
            if r and r["pump_m3_per_hour"]:
                m3 = hours * r["pump_m3_per_hour"]
        LEDGER.log_action(rid, followed=True, actual_day=date.today(),
                          actual_m3=m3, source="farmer",
                          note=f"{hours} soat" if hours is not None else None)

    if hours is None:
        await update.message.reply_text(
            "Yozib oldim. Rahmat!\n\n"
            "Nasos necha soat ishladi? Shunday yozing: /bajardim 9")
    else:
        await update.message.reply_text(f"Yozib oldim: {hours:.0f} soat. Rahmat!")


def main() -> None:
    token = os.environ["TELEGRAM_TOKEN"]
    app = Application.builder().token(token).build()
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CROP: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_crop)],
            HECTARES: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_hectares)],
            METHOD: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_method)],
            LOCATION: [MessageHandler(filters.LOCATION, got_location)],
        },
        fallbacks=[CommandHandler("start", start)],
    ))
    app.add_handler(CommandHandler("suv", suv))
    app.add_handler(CommandHandler("tejaldi", tejaldi))
    app.add_handler(CommandHandler("bajardim", bajardim))
    log.info("SUV AI bot v%s starting", __version__)
    app.run_polling()


if __name__ == "__main__":
    main()
