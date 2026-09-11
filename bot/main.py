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
from suv.climate import STATIONS, nearest_station, season
from suv.clock import today as today_tashkent
from suv.crop import CROPS
from suv.field_shape import MAX_VERTICES
from suv.field_shape import area_ha as polygon_area_ha
from suv.field_photo import can_show_photo
from suv.field_shape import from_geojson_ring
from suv.field_shape import inlet_edge as polygon_inlet_edge
from suv.field_shape import inlet_options as polygon_inlet_options
from suv.field_shape import to_geojson_ring
from suv.field_shape import validate as validate_shape
from suv.field_status import (DRAW_ACTION_UZ, LIST_HEADER, PHOTO_BLOCKED,
                              Status, assemble, cost_section,
                              field_list_label, overall_status,
                              photo_section, render_card, uniformity_section,
                              water_section, weather_section)
from suv.boundary_import import parse_file as parse_boundaries
from suv.boundary_import import seed as seed_boundaries
from suv.landsat import enabled as landsat_enabled
from suv.ledger import Ledger
from suv.messages import (recommendation_text, salinity_warning,
                          savings_text, why_text)
from suv.schedule import Field, recommend, simulate
from suv.soil import SOILS, WaterBalanceState
from suv.spray import build_section as spray_build_section
from suv.weather import fetch_elevation, fetch_forecast, fetch_hourly

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
# httpx пишет полный URL каждого запроса — вместе с токеном бота.
# journalctl на VPS или веб-логи Railway не должны содержать токен.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("suv.bot")

CROP, PLANTING, HECTARES, METHOD, SOIL, LOCATION = range(6)
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

CROP_EMOJI = {"cotton": "🌱", "winter_wheat": "🌾", "onion": "🧅",
              "tomato": "🍅", "apple": "🍎", "grape": "🍇",
              "apricot": "🍑", "alfalfa": "🌿", "barley": "🌾"}
# Все культуры движка, включая многолетники: сад и виноградник раньше
# заводились только агрономом, и садовод из жюри был вынужден выбирать
# чужую культуру — совет получался неверным от Kc до корней.
CROP_ORDER = ("cotton", "winter_wheat", "barley", "onion", "tomato",
              "alfalfa", "apple", "grape", "apricot")


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
BTN_KABINET = "🖥 Kabinet"

MAIN_MENU = ReplyKeyboardMarkup(
    [[BTN_SUV, BTN_BAJARDIM], [BTN_TEJALDI, BTN_YORDAM]],
    resize_keyboard=True)

# Экран «Dala holati» и всё, что с него достижимо: обводка контура,
# сторона входа воды, снимок поля, заметки с фото, окна опрыскивания,
# «почему такой совет». Правило списка ПЕРЕВЁРНУТО 09.09.2026: пусто =
# открыто ВСЕМ, как у ALLOWED_CHAT_IDS выше. Раньше пусто значило «ни у
# кого», и это молча выключало главную функцию продукта — без контура
# NDVI считается по квадрату 200x200 м вместе с дорогой и соседом.
# Закрыть обратно = вписать сюда chat_id тех, кому оставляем.
_FIELD_STATUS: set[int] = _ids_from_env("FIELD_STATUS_CHAT_IDS")

# Веб-кабинет: подробный разбор поля на большом экране. Открывается
# кнопкой Mini App — личность подтверждает подпись Telegram, паролей нет
# (см. suv/webauth.py). Гейт свой, отдельный от «Dala holati»: обкатывать
# их независимо дешевле, чем чинить оба сразу.
CABINET_URL = os.environ.get("CABINET_URL", "").strip()
_CABINET: set[int] = _ids_from_env("CABINET_CHAT_IDS")

# Куда пересылать вопросы фермеров. Пусто = вопрос всё равно получает
# ответ, просто не уезжает никуда: молчание в ответ на живой вопрос —
# худшее из двух зол, и терять сообщение из-за незаполненной переменной
# нельзя. Оно в любом случае остаётся в логе.
_ADMIN_CHAT: set[int] = _ids_from_env("ADMIN_CHAT_IDS")

MAIN_MENU_DALA = ReplyKeyboardMarkup(
    [[BTN_SUV, BTN_BAJARDIM], [BTN_TEJALDI, BTN_YORDAM], [BTN_DALA]],
    resize_keyboard=True)


def _menu(chat_id: int) -> ReplyKeyboardMarkup:
    """Главное меню чата: четыре базовые кнопки всем, экраны закрытых
    демо — только своим чатам.

    Раньше здесь выбиралась одна из двух готовых констант. С третьей
    фичей это дало бы четыре комбинации клавиатуры и ошибку в одной из
    них рано или поздно, поэтому нижний ряд собирается по флагам.
    MAIN_MENU и MAIN_MENU_DALA оставлены: на них ссылаются тесты и они
    описывают два опорных состояния меню.
    """
    rows = [[BTN_SUV, BTN_BAJARDIM], [BTN_TEJALDI, BTN_YORDAM]]
    extra: list = []
    if _field_status_open(chat_id):
        extra.append(BTN_DALA)
    # Кнопка кабинета — ОБЫЧНАЯ текстовая, не web_app. Причина
    # проверена на Telegram Desktop 9.6: приложениям из reply-клавиатуры
    # он не передаёт tgWebAppData (в hash приезжают только параметры
    # темы), и кабинет не может подтвердить личность. Поэтому нажатие
    # обрабатывает kabinet(), отвечающий inline-кнопкой — этот способ
    # запуска несёт подпись на всех клиентах.
    if chat_id in _CABINET and CABINET_URL:
        extra.append(BTN_KABINET)
    if extra:
        rows.append(extra)
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

HOUR_OPTIONS = (6, 8, 9, 10, 12)
MAX_HOURS = 48.0  # опечатка вида /bajardim 999 не должна попадать в KPI

# Подтверждение полива принимается только против свежей рекомендации:
# отметка против прошлонедельной — шум, а не данные.
REC_MAX_AGE_DAYS = 7

# Утренний автопуш: фермер читает до выхода в поле.
PUSH_HOUR_TASHKENT = 6
# Сообщение приходит КАЖДОЕ утро — решение Амира 17.08.2026: тишина
# читается фермером как «бот умер», а не как «всё в порядке». Единица
# здесь значит «молчим, только если совет уже уходил сегодня» (кнопкой
# или этим же пушем — от повтора защищает). Вернуть прежний ритм «раз в
# три дня» можно, снова поставив 3.
PUSH_QUIET_DAYS = 1

# Bosib qoldirsa ham — ro'yxatdan o'tish savoliga javob bermay, menyu
# tugmasini bossa, vizard shu yerda to'xtab qolmasligi kerak.
_MENU_PATTERN = "|".join(re.escape(b) for b in
                         (BTN_SUV, BTN_BAJARDIM, BTN_TEJALDI, BTN_YORDAM))
# Кнопка демо-экрана вычитается из вопросов мастера ТОЛЬКО у демо-чатов:
# filters.Chat(пустое множество) не совпадает ни с кем, так что для всех
# остальных регистрация ведёт себя ровно как до этой ветки.
_DALA_TEXT = filters.Regex(f"^{re.escape(BTN_DALA)}$")
# filters.Chat(пустое множество) не совпадает НИ С КЕМ, поэтому при
# открытом гейте фильтр по чату не навешивается вовсе.
DALA_FILTER = (_DALA_TEXT if not _FIELD_STATUS
               else _DALA_TEXT & filters.Chat(_FIELD_STATUS))
MENU_FILTER = filters.Regex(f"^({_MENU_PATTERN})$") | DALA_FILTER


def _authorized(update: Update) -> bool:
    return not _ALLOWED or update.effective_chat.id in _ALLOWED


def _field_status_open(chat_id: int) -> bool:
    """Доступен ли чату экран поля и всё, что с него достижимо.

    Та же конвенция, что у _authorized: пустой список = открыто всем.
    Одно место, где решается вопрос, — иначе шесть разных проверок
    разъезжаются, и одна из них рано или поздно останется закрытой.
    """
    return not _FIELD_STATUS or chat_id in _FIELD_STATUS


async def _reject(update: Update) -> None:
    await update.message.reply_text(
        "Bu bot yopiq sinovda. Qatnashish uchun egasiga murojaat qiling.")


def _owner_fields(chat_id: int, include_archived: bool = False) -> list:
    import sqlite3
    cond = "" if include_archived else " AND archived_at IS NULL"
    with sqlite3.connect(LEDGER.path) as c:
        c.row_factory = sqlite3.Row
        return c.execute("SELECT * FROM fields WHERE owner_chat_id=?" + cond +
                         " ORDER BY field_id", (chat_id,)).fetchall()


def _all_fields() -> list:
    import sqlite3
    with sqlite3.connect(LEDGER.path) as c:
        c.row_factory = sqlite3.Row
        return c.execute("SELECT * FROM fields WHERE archived_at IS NULL "
                         "ORDER BY field_id").fetchall()


# Потолок полей на чат: не тариф, а предохранитель от случайного зоопарка
# и от чужого скрипта, молотящего /start. Гиганту поля заводим импортом.
MAX_FIELDS_PER_CHAT = 10


def _next_field_id(chat: int) -> str:
    """TG-{chat}, затем TG-{chat}-2, -3… Архивные id остаются занятыми:
    к старому id привязан журнал, и «новое поле №2» не имеет права
    унаследовать чужую историю."""
    taken = {r["field_id"]
             for r in _owner_fields(chat, include_archived=True)}
    if f"TG-{chat}" not in taken:
        return f"TG-{chat}"
    n = 2
    while f"TG-{chat}-{n}" in taken:
        n += 1
    return f"TG-{chat}-{n}"


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
    # Мастер регистрации перехватывает текстовые кнопки обводки как
    # ответы на свои вопросы. Раз начали заново — режим обводки закрыт.
    ctx.user_data.pop("draw", None)
    ctx.user_data.pop("draw_pending", None)
    rows = _owner_fields(chat)
    # Раньше при заведённых агрономом полях мастер отвечал отказом:
    # повторная регистрация ПЕРЕЗАПИСЫВАЛА единственное TG-{chat} полями
    # по умолчанию. Теперь id последовательные и мастер спрашивает почву
    # и высоту сам — /start честно ДОБАВЛЯЕТ поле (фаза 1 карты OneSoil).
    if len(rows) >= MAX_FIELDS_PER_CHAT:
        await update.message.reply_text(
            f"Sizda allaqachon {len(rows)} ta dala bor — bu chegara.\n"
            "Yana qo'shish kerak bo'lsa — Amirga yozing.",
            reply_markup=_menu(chat))
        return ConversationHandler.END
    intro = "Assalomu alaykum!\n\nMen sizga qachon va qancha sug'orish kerakligini aytaman.\n\n"
    if rows:
        names = ", ".join(r["name"] for r in rows)
        intro = (f"Ro'yxatda {len(rows)} ta dalangiz bor: {names}.\n"
                 "Yana bitta dala qo'shamiz.\n\n")

    labels = [_crop_label(k) for k in CROP_ORDER]
    kb = [labels[i:i + 3] for i in range(0, len(labels), 3)]
    await update.message.reply_text(
        intro + "Dalangizda nima ekilgan?",
        reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True,
                                         resize_keyboard=True))
    return CROP


# Возраст многолетника вместо месяца сева: у сада и лозы дата «сева» —
# это год посадки, и от него движок считает, выросли ли корни
# (PERENNIAL_ESTABLISHED_YEARS). Точный год фермер помнит не всегда,
# диапазона хватает: после трёх лет корни считаются взрослыми.
AGE_BY_ANSWER = {"1 yosh": 1, "2 yosh": 2, "3 yosh": 3,
                 "5 yosh": 5, "10 yosh": 10, "15+ yosh": 15}


async def got_crop(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    name = update.message.text.strip()
    key = next((k for k in CROP_ORDER if _crop_label(k) == name), None)
    if key is None:
        await update.message.reply_text("Iltimos, ro'yxatdan tanlang.")
        return CROP
    ctx.user_data["crop"] = key
    if CROPS[key].perennial:
        ages = list(AGE_BY_ANSWER)
        kb = [ages[i:i + 3] for i in range(0, len(ages), 3)]
        await update.message.reply_text(
            "Daraxt (tok) necha yoshda?",
            reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True,
                                             resize_keyboard=True))
        return PLANTING
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
    crop = CROPS[ctx.user_data["crop"]]
    today = today_tashkent()
    if crop.perennial:
        years = AGE_BY_ANSWER.get(name)
        if years is None:
            await update.message.reply_text("Iltimos, yoshini ro'yxatdan tanlang.")
            return PLANTING
        month, day = crop.typical_sowing
        ctx.user_data["planting"] = date(today.year - years, month, day)
    else:
        if name not in MONTHS_UZ:
            await update.message.reply_text("Iltimos, oyni ro'yxatdan tanlang.")
            return PLANTING
        month = MONTHS_UZ.index(name) + 1
        year = today.year if month <= today.month else today.year - 1
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


# Тип почвы фермерским языком. Спрашивать «суглинок или супесь» у
# дехканина бессмысленно, а знать надо: TAW и RAW считаются напрямую из
# него, и песок против глины — это разница запаса влаги почти вдвое.
# Здесь стояло жёсткое soil_key="loam" для КАЖДОГО поля, заведённого
# через бота, — то есть для всех, кто пришёл сам.
SOIL_FAST = "Tez singadi"
SOIL_MID = "O'rtacha"
SOIL_SLOW = "Uzoq turadi"
SOIL_BY_ANSWER = {SOIL_FAST: "sand", SOIL_MID: "loam", SOIL_SLOW: "clay"}


async def got_method(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    t = update.message.text
    ctx.user_data["method"] = ("drip" if "Tomchi" in t
                               else "sprinkler" if "Yomg" in t else "furrow")
    await update.message.reply_text(
        "Sug'organingizdan keyin suv yerga qanday singadi?",
        reply_markup=ReplyKeyboardMarkup(
            [[SOIL_FAST], [SOIL_MID], [SOIL_SLOW]],
            one_time_keyboard=True, resize_keyboard=True))
    return SOIL


async def got_soil(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    key = SOIL_BY_ANSWER.get((update.message.text or "").strip())
    if key is None:
        await update.message.reply_text(
            "Uchta javobdan birini tanlang.",
            reply_markup=ReplyKeyboardMarkup(
                [[SOIL_FAST], [SOIL_MID], [SOIL_SLOW]],
                one_time_keyboard=True, resize_keyboard=True))
        return SOIL
    ctx.user_data["soil"] = key
    await update.message.reply_text(
        "Oxirgi qadam: dalangiz joylashuvini yuboring.",
        reply_markup=ReplyKeyboardMarkup(
            [[KeyboardButton(LOCATION_LABEL, request_location=True)]],
            one_time_keyboard=True, resize_keyboard=True))
    return LOCATION


async def got_location(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    loc = update.message.location
    chat = update.effective_chat.id
    fid = _next_field_id(chat)
    n_before = len(_owner_fields(chat))
    # Имя различимое сразу: два «Mening dalam» в списке «Suv berdim»
    # неотличимы, а /nom есть не у всех в привычке.
    name = "Mening dalam" if n_before == 0 else f"Dala {n_before + 1}"

    # Высота — из точки, которую фермер только что прислал, а не 500 м
    # для всех подряд: она входит в ET0 через атмосферное давление
    # (FAO-56 ур. 7). Не ответили — остаётся прежнее умолчание.
    elev = await asyncio.to_thread(fetch_elevation, loc.latitude, loc.longitude)
    if elev is None:
        log.info("%s: высота не получена, беру 500 м", fid)

    LEDGER.upsert_field(
        field_id=fid, name=name, owner_chat_id=chat,
        hectares=ctx.user_data["hectares"], lat=loc.latitude, lon=loc.longitude,
        elevation_m=500.0 if elev is None else elev,
        crop_key=ctx.user_data["crop"],
        soil_key=ctx.user_data.get("soil", "loam"),
        planting_date=ctx.user_data["planting"].isoformat(),
        irrigation_method=ctx.user_data["method"],
        water_table_depth_m=0.0, baseline_m3_per_ha=None,
        baseline_interval_days=30, last_irrigation_date=None)

    await update.message.reply_text(
        f"«{name}» saqlandi.\n\n"
        "Har kuni ertalab sizga sug'orish bo'yicha xabar yuboraman.\n"
        f"Hozir tekshirish uchun pastdagi “{BTN_SUV}” tugmasini bosing.\n"
        "Nomini o'zgartirish: /nom · Ro'yxatdan olish: /ochirish",
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
        water_table_depth_m=row["water_table_depth_m"] or 0.0,
        # Колонки дописаны в августе 2026; на строке из старой базы их
        # может не быть — тогда умолчания: доля по способу полива,
        # съём не задан.
        wetted_fraction=(row["wetted_fraction"]
                         if "wetted_fraction" in row.keys() else None),
        harvest_start=_opt_date(row, "harvest_start"),
        harvest_end=_opt_date(row, "harvest_end"))


def _opt_date(row, col: str) -> date | None:
    if col not in row.keys() or not row[col]:
        return None
    return date.fromisoformat(row[col])


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


def _rewind(fld: Field, last_irr: date | None, series: list, gap: int,
            today: date) -> tuple[WaterBalanceState, list]:
    """Прокрутить историю по готовому ряду погоды и отдать прогноз.

    В день полива профиль считается наполненным (дефицит 0). Это НЕ
    самоочевидно, и однажды это уже пытались «починить»: движок
    ограничивает один капельный сеанс 25 мм net (MAX_APPLICATION_MM), а
    порог RAW у яблони под 90 мм, откуда следовал вывод, что после
    полива остаётся долг ~72 мм. Проверка на живой погоде показала, что
    вывод неверен, потому что неверна посылка: Фаррух поливает НЕ по
    порогу, а по расписанию раз в четыре дня, и за эти четыре дня
    накапливается ~23 мм — их сеанс в 25.9 мм net перекрывает целиком.
    Подстановка RAF-остатка давала 2778 м³ на две недели против 1808 м³
    настоящей потребности: перелив в полтора раза, а на засоленной земле
    лишняя перколяция поднимает соль к корням.

    Остаётся известное ограничение, и оно противоположного знака: если
    фермер идёт по РАСТЯНУТЫМ интервалам самого бота, то к моменту
    полива дефицит и правда больше того, что даёт один сеанс капли, и
    ноль его занижает. Честно это лечится не догадкой о дефиците, а
    прокруткой баланса СКВОЗЬ все подтверждённые поливы — по журналу
    actions, без сброса состояния. Пока такой прокрутки нет, ноль честнее
    выдуманного остатка.
    """
    history, future = series[:gap], series[gap:]
    if not future:  # ряд обрезался на дырах в архиве — прогноза нет
        raise ValueError("weather series has no forecast part")

    state = WaterBalanceState(0.0, 0.20)
    if history and last_irr:
        # Стартуем с первого дня ИСТОРИИ, а не с last_irr: при разрыве
        # дольше 92 дней (предел архива) история покрывает последние 92
        # дня, и метки last_irr..last_irr+91 сдвигали фазы культуры и
        # Kc относительно погоды реальных дат.
        plan = simulate(fld, history, state,
                        today - timedelta(days=len(history)),
                        apply_irrigation=False)
        last = plan[-1]
        # Глубину корней берём из плана напрямую: taw_mm у капли умножен
        # на долю смачивания, и деление его на запас-на-метр давало бы
        # 0,6 м вместо 1,5 — следующий шаг баланса «растил» бы корни
        # заново и размывал накопленный дефицит.
        zr = last.root_depth_m or 0.20
        state = WaterBalanceState(last.depletion_mm, zr)
    return state, future


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
    return _rewind(fld, last_irr, series, gap, today)


# Поле без даты последнего полива считается «только что политым» — это
# документированный компромисс, но молчать о нём нельзя: оптимистичный
# «полив не требуется» на пустых данных — прямой путь засушить поле.
NO_ANCHOR_UZ = ("⚠️ Oxirgi sug'orish sanasi noma'lum — hisob taxminiy.\n"
                "Sug'organingizdan keyin «✅ Suv berdim» tugmasini bosing, "
                "hisob aniqlashadi.")
NO_ANCHOR_RU = ("⚠️ Дата последнего полива неизвестна — расчёт приблизительный "
                "и может недооценивать потребность в воде.")


def _field_polygon(row) -> list[list[float]] | None:
    """Контур поля как кольцо GeoJSON, если фермер его обвёл.

    Битый JSON не должен ронять рекомендацию: без контура расчёт просто
    возвращается к квадрату вокруг точки, как было до дня 2.
    """
    raw = row["polygon_geojson"] if "polygon_geojson" in row.keys() else None
    if not raw:
        return None
    import json
    try:
        ring = json.loads(raw)
    except (ValueError, TypeError):
        log.warning("контур %s не читается как JSON", row["field_id"])
        return None
    return ring if isinstance(ring, list) and len(ring) >= 4 else None


def _compute_rec(row, today: date):
    """Полный расчёт по одному полю: спутник, warm-start, рекомендация.

    Общий путь для кнопки «Suv holati» и утреннего автопуша — совет
    обязан быть одинаковым, каким бы способом фермер его ни получил.
    Возвращает (rec, pump, anchored, degraded): anchored=False — расчёту
    не от чего отталкиваться; degraded=True — живая погода не пришла и
    расчёт сделан по климатическим нормам, о чём фермеру говорится
    прямо, а журнал KPI такую рекомендацию не записывает.
    """
    fld = _build_field(row)

    from suv.enrich import attach_ndvi
    # Обведённый контур сразу идёт в дело: без него спутник усредняет
    # NDVI по квадрату 200x200 м вокруг точки и может захватить соседнюю
    # культуру и дорогу. С контуром замер идёт ровно по полю.
    log.info("%s: %s", fld.field_id,
             attach_ndvi(fld, polygon=_field_polygon(row)))

    last_irr = _last_irrigation(fld.field_id,
                                row["last_irrigation_date"])
    degraded = False
    try:
        state, forecast = _warm_state(fld, last_irr, today)
    except Exception as exc:  # noqa: BLE001
        # Погода не пришла. Раньше сюда молча подставлялось «поле только
        # что полито» + нормали: реальный дефицит стирался в ноль,
        # urgent не наступал, и утренний пуш «поливай сегодня» тонул —
        # ровно в тот день, когда поле сохло. Теперь та же отмотка от
        # последнего полива идёт по климатическим НОРМАМ, а сообщение
        # честно предупреждает о приблизительности (degraded).
        log.warning("weather feed failed for %s: %s — считаю по нормам",
                    fld.field_id, exc)
        degraded = True
        gap = max(0, min((today - last_irr).days if last_irr else 0, 92))
        series = season(nearest_station(fld.lat, fld.lon),
                        today - timedelta(days=gap), gap + 14)
        state, forecast = _rewind(fld, last_irr, series, gap, today)

    # Прежний расход берётся ИЗ ПОЛЯ, а не из умолчаний движка. Раньше
    # здесь стоял голый recommend(fld, forecast, state, today), и на
    # любое поле садились «типичные» 30 дней по 1100 м³/га: у сада
    # Olmazor вместо его собственных 228,6 м³/га раз в 4 дня, а у
    # виноградника Uzumzor, где расхода никто не называл, база бралась
    # из воздуха. Фермеру эти числа сегодня не показывают — но они лежат
    # в объекте и ждут первого, кто их напечатает.
    rec = recommend(
        fld, forecast, state, today,
        baseline_interval_days=row["baseline_interval_days"],
        baseline_application_m3_per_ha=row["baseline_m3_per_ha"],
    )

    pump = None
    if row["pump_kwh_per_hour"] and row["pump_m3_per_hour"]:
        from suv.economics import PumpProfile
        pump = PumpProfile(row["pump_kwh_per_hour"], row["pump_m3_per_hour"],
                           row["pump_cost_per_hour_uzs"] or 0.0,
                           row["pump_lift_m"] or 0.0)
    return rec, pump, last_irr is not None, degraded


WEATHER_DOWN_UZ = ("⚠️ Ob-havo xizmati javob bermadi — hisob ko'p yillik "
                   "me'yorlar bo'yicha, taxminiy.")
WEATHER_DOWN_RU = ("⚠️ Прогноз погоды недоступен — расчёт по климатическим "
                   "нормам, приблизительный.")


def _rec_message(rec, pump, lang: str, anchored: bool = True,
                 degraded: bool = False) -> str:
    msg = recommendation_text(rec, lang, pump=pump)
    warn = salinity_warning(rec.plan[0].salinity if rec.plan else "unknown", lang)
    if warn:
        msg += "\n\n" + warn
    if degraded:
        msg += "\n\n" + (WEATHER_DOWN_UZ if lang == "uz" else WEATHER_DOWN_RU)
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

    today = today_tashkent()
    ctx.user_data.setdefault("last_rec_ids", {})
    for row in rows:
        # Расчёт — в поток: внутри до четырёх сетевых вызовов (токен,
        # снимок, каталог сцен, погода) по 20-60 с таймаутом каждый. В
        # корутине они держали весь event loop: пока считалось одно поле,
        # бот не отвечал никому — ни на «✅ Suv berdim», ни на пуш.
        rec, pump, anchored, degraded = await asyncio.to_thread(
            _compute_rec, row, today)
        # Пишем в журнал и деградированный совет тоже. Пробовали не
        # писать — «это не тот расчёт, за который мы отвечаем», — и
        # порвали цепочку: без rid в last_rec_ids кнопка «✅ Suv berdim»
        # утыкалась в позавчерашнюю рекомендацию, has_action отвечал
        # «уже записано», и полив фермера пропадал вместе с якорем
        # водного баланса. Правило журнала прямое: храним то, что
        # СКАЗАЛИ фермеру. Сказали — значит записываем; о приблизительности
        # предупреждает сам текст сообщения.
        rid = None
        if not observer:
            rid = LEDGER.log_recommendation(rec, __version__)
            ctx.user_data["last_rec_ids"][rec.field.field_id] = rid
        await update.message.reply_text(
            _rec_message(rec, pump, lang, anchored=anchored,
                         degraded=degraded),
            reply_markup=_why_markup(update.effective_chat.id,
                                     rec.field.field_id, lang, rid=rid))


# «Почему такой совет?» — закрытое демо (_FIELD_STATUS): фермеру кнопка
# уедет после обкатки. Инлайн-клавиатура не трогает постоянное меню —
# reply-клавиатура остаётся от прежних сообщений.
WHY_BTN_UZ = "🤔 Nega bunday maslahat?"
WHY_BTN_RU = "🤔 Почему такой совет?"
FB_UP = "👍 Foydali"
FB_DOWN = "👎 Xato"


def _why_markup(chat: int, field_id: str, lang: str = "uz",
                rid: int | None = None):
    """Кнопки под советом: «почему» + оценка одним тапом (при известном
    id рекомендации — голос привязывается к конкретному совету)."""
    if not _field_status_open(chat):
        return _menu(chat)
    label = WHY_BTN_UZ if lang == "uz" else WHY_BTN_RU
    rows = [[InlineKeyboardButton(label, callback_data=f"why:{field_id}")]]
    if rid is not None:
        rows.append([InlineKeyboardButton(FB_UP, callback_data=f"fb:up:{rid}"),
                     InlineKeyboardButton(FB_DOWN, callback_data=f"fb:down:{rid}")])
    return InlineKeyboardMarkup(rows)


async def feedback_callback(update: Update,
                            ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """👍/👎 под советом — «Rate zones» OneSoil на нашем жанре.

    Голос пишется в журнал (advice_feedback), не в метрику точности:
    эталона по-прежнему нет, и 👍 не делает совет верным. Это сырьё для
    разговора с фермером и для разборов."""
    query = update.callback_query
    chat = update.effective_chat.id
    try:
        _, verdict, raw = query.data.split(":", 2)
        rid = int(raw)
    except ValueError:
        await query.answer()
        return
    fid = LEDGER.recommendation_field(rid)
    if fid is None:
        await query.answer("Eskirgan maslahat.")
        return
    LEDGER.add_feedback(rid, fid, chat, verdict)
    await query.answer("Rahmat! Yozib olindi.")
    # Кнопки оценки сворачиваются в выбранную; «почему» остаётся.
    chosen = FB_UP if verdict == "up" else FB_DOWN
    try:
        await query.edit_message_reply_markup(InlineKeyboardMarkup([
            [InlineKeyboardButton(WHY_BTN_UZ, callback_data=f"why:{fid}")],
            [InlineKeyboardButton(f"✅ {chosen}",
                                  callback_data=f"fb:{verdict}:{rid}")]]))
    except Exception:  # noqa: BLE001 — правка клавиатуры не критична
        pass


async def why_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Детерминированное объяснение совета — из того же расчёта, что и
    сам совет. Никакой генерации: каждую строку можно проверить по
    журналу. Это сознательная замена AI-чату (решение 09.09.2026)."""
    query = update.callback_query
    await query.answer()
    chat = update.effective_chat.id
    if not _field_status_open(chat):
        return
    fid = query.data.split(":", 1)[1]
    row = _field_row(fid)
    if row is None:
        await query.message.reply_text("Dala topilmadi. /suv ni qayta yuboring.")
        return
    lang = "uz" if row["owner_chat_id"] == chat else "ru"
    rec, _pump, _anchored, degraded = await asyncio.to_thread(
        _compute_rec, row, today_tashkent())
    last_irr = _last_irrigation(fid, row["last_irrigation_date"])
    await query.message.reply_text(why_text(rec, last_irr, lang,
                                            degraded=degraded))


# ------------------------------------------------------------- заметки
#
# Фермер шлёт фото — бот подшивает его к полю с датой и подписью;
# локация, присланная следом, приклеивается к той же заметке. Сам файл
# живёт у Телеграма (file_id), мы храним ссылку и контекст. Закрытое
# демо, как всякая новая поверхность.
NOTE_ATTACH_WINDOW_S = 600.0


def _save_note(ctx: ContextTypes.DEFAULT_TYPE, chat: int, row,
               file_id: str | None, caption: str | None) -> str:
    nid = LEDGER.add_note(row["field_id"], chat, today_tashkent(),
                          file_id, caption)
    ctx.user_data["last_note"] = (nid, time.monotonic())
    d = today_tashkent()
    return (f"📝 {row['name']}: yozib olindi ({d.day:02d}.{d.month:02d}).\n"
            "Joyni biriktirish uchun lokatsiya yuboring.")


async def photo_note(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat.id
    if not _field_status_open(chat) or not _authorized(update):
        return
    rows = _owner_fields(chat)
    if not rows:
        await update.message.reply_text("Avval /start buyrug'ini yuboring.")
        return
    file_id = update.message.photo[-1].file_id
    caption = (update.message.caption or "").strip() or None
    if len(rows) == 1:
        await update.message.reply_text(
            _save_note(ctx, chat, rows[0], file_id, caption))
        return
    ctx.user_data["pending_note"] = {"file_id": file_id, "caption": caption,
                                     "at": time.monotonic()}
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton(r["name"], callback_data=f"notefld:{r['field_id']}")]
         for r in rows])
    await update.message.reply_text("Qaysi dala uchun?", reply_markup=kb)


async def note_field_callback(update: Update,
                              ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat = update.effective_chat.id
    pending = ctx.user_data.pop("pending_note", None)
    if not pending or time.monotonic() - pending["at"] > NOTE_ATTACH_WINDOW_S:
        await query.edit_message_text("Eskirdi — rasmni qaytadan yuboring.")
        return
    fid = query.data.split(":", 1)[1]
    row = _field_row(fid)
    if row is None or row["owner_chat_id"] != chat:
        await query.edit_message_text("Dala topilmadi.")
        return
    await query.edit_message_text(
        _save_note(ctx, chat, row, pending["file_id"], pending["caption"]))


async def _maybe_attach_note_location(update: Update,
                                      ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    """Локация после фото — точка заметки. Одноразово и только в окне:
    локация, присланная позже по другому поводу, место не переписывает."""
    last = ctx.user_data.get("last_note")
    if not last or time.monotonic() - last[1] > NOTE_ATTACH_WINDOW_S:
        return False
    msg = update.effective_message
    loc = getattr(msg, "location", None)
    if loc is None:
        return False
    ctx.user_data.pop("last_note", None)
    if LEDGER.attach_note_location(last[0], loc.latitude, loc.longitude):
        await msg.reply_text("📍 Joylashuv eslatmaga biriktirildi.")
        return True
    return False


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
                 if _field_status_open(chat) else "")
    await update.message.reply_text(
        f"{BTN_SUV} — bugungi sug'orish tavsiyasi\n"
        f"{BTN_BAJARDIM} — suv berganingizni belgilash\n"
        f"{BTN_TEJALDI} — mavsum davomida tejalgan suv\n"
        f"{dala_line}\n"
        "/start — yangi dala qo'shish\n"
        "/nom — dala nomini o'zgartirish\n"
        "/ochirish — dalani ro'yxatdan olish (tarix saqlanadi)\n\n"
        "Savolingiz bo'lsa — shu yerga yozing, Amir javob beradi.",
        reply_markup=_menu(chat))


# ------------------------------------------------- импорт границ файлом
#
# Агроном хозяйства присылает боту файл границ (KML/KMZ/GeoJSON/ZIP c
# shapefile) — и получает все поля разом, вместо тридцати проходов
# мастера. Одна культура и один способ полива на файл: клинья
# однородны, исключения правятся точечно после посева. Четыре вопроса —
# четыре тапа: культура -> дата (месяц или возраст) -> почва -> полив.
IMPORT_EXTS = (".kml", ".kmz", ".geojson", ".json", ".zip")
IMPORT_MAX_BYTES = 15 * 1024 * 1024
IMPORT_MAX_FIELDS = 50          # за один файл через бот; больше — CLI
IMPORT_TTL_S = 900.0
PERENNIAL_AGES = (1, 2, 3, 5, 10, 15)


def _imp_label(key: str) -> str:
    return f"{CROP_EMOJI.get(key, '🌱')} {CROPS[key].name_uz}"


def _imp_pending(ctx) -> dict | None:
    pend = ctx.user_data.get("pending_import")
    if not pend or time.monotonic() - pend["at"] > IMPORT_TTL_S:
        ctx.user_data.pop("pending_import", None)
        return None
    return pend


async def import_doc(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await _reject(update)
        return
    doc = update.message.document
    fname = (doc.file_name or "").strip()
    if not fname.lower().endswith(IMPORT_EXTS):
        await update.message.reply_text(
            "Dala chegaralari faylini yuboring: KML, KMZ, GeoJSON yoki "
            "ZIP (shapefile).")
        return
    if doc.file_size and doc.file_size > IMPORT_MAX_BYTES:
        await update.message.reply_text("Fayl juda katta (15 MB gacha).")
        return
    tg_file = await doc.get_file()
    data = bytes(await tg_file.download_as_bytearray())
    fields, problems = await asyncio.to_thread(parse_boundaries, data, fname)
    if not fields:
        text = "Fayldan dala chiqmadi." if not problems else             "Fayldan dala chiqmadi:\n" + "\n".join(f"• {p}" for p in problems[:5])
        await update.message.reply_text(text)
        return
    if len(fields) > IMPORT_MAX_FIELDS:
        problems.append(f"Bot orqali bir faylda {IMPORT_MAX_FIELDS} tagacha "
                        f"dala: birinchi {IMPORT_MAX_FIELDS} tasi olinadi.")
        fields = fields[:IMPORT_MAX_FIELDS]

    ctx.user_data["pending_import"] = {"fields": fields,
                                       "at": time.monotonic()}
    total = sum(f.area_ha for f in fields)
    listing = "\n".join(f"• {f.name} — {f.area_ha:g} ga" for f in fields[:6])
    if len(fields) > 6:
        listing += f"\n… va yana {len(fields) - 6} ta"
    warn = ""
    if problems:
        warn = "\n\n⚠️ " + "\n⚠️ ".join(problems[:3])
    keys = list(CROPS)
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton(_imp_label(k), callback_data=f"impc:{k}")
          for k in keys[i:i + 2]] for i in range(0, len(keys), 2)])
    await update.message.reply_text(
        f"Fayldan {len(fields)} ta dala o'qildi, jami {total:.1f} ga:\n"
        f"{listing}{warn}\n\nHammasida nima ekilgan?", reply_markup=kb)


async def import_flow_callback(update: Update,
                               ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat = update.effective_chat.id
    pend = _imp_pending(ctx)
    if pend is None:
        await query.edit_message_text("Eskirdi — faylni qaytadan yuboring.")
        return
    kind, value = query.data.split(":", 1)

    if kind == "impc":
        if value not in CROPS:
            return
        pend["crop"] = value
        crop = CROPS[value]
        if crop.perennial:
            kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton(f"{a} yosh", callback_data=f"impa:{a}")
                  for a in PERENNIAL_AGES[i:i + 3]]
                 for i in range(0, len(PERENNIAL_AGES), 3)])
            await query.edit_message_text(
                f"{crop.name_uz}: daraxt/tok necha yoshda?", reply_markup=kb)
        else:
            kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton(MONTHS_UZ[m - 1], callback_data=f"impm:{m}")
                  for m in range(r, r + 3)] for r in (1, 4, 7, 10)])
            await query.edit_message_text("Qaysi oyda ekilgan?",
                                          reply_markup=kb)
        return

    if kind in ("impm", "impa"):
        crop = CROPS.get(pend.get("crop", ""))
        if crop is None:
            return
        today = today_tashkent()
        if kind == "impa":
            years = int(value)
            month, day = crop.typical_sowing
            pend["planting"] = date(today.year - years, month, day)
        else:
            month = int(value)
            year = today.year if month <= today.month else today.year - 1
            day = crop.typical_sowing[1] if month == crop.typical_sowing[0] else 15
            pend["planting"] = date(year, month, day)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(SOIL_FAST, callback_data="imps:sand")],
            [InlineKeyboardButton(SOIL_MID, callback_data="imps:loam")],
            [InlineKeyboardButton(SOIL_SLOW, callback_data="imps:clay")]])
        await query.edit_message_text(
            "Tuproq suvni qanday ushlaydi? (hammasi uchun)", reply_markup=kb)
        return

    if kind == "imps":
        if value not in SOILS:
            return
        pend["soil"] = value
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(METHOD_LABELS["furrow"], callback_data="impw:furrow")],
            [InlineKeyboardButton(METHOD_LABELS["sprinkler"], callback_data="impw:sprinkler")],
            [InlineKeyboardButton(METHOD_LABELS["drip"], callback_data="impw:drip")]])
        await query.edit_message_text("Qanday sug'oriladi? (hammasi uchun)",
                                      reply_markup=kb)
        return

    if kind == "impw":
        if value not in ("furrow", "sprinkler", "drip") or "soil" not in pend:
            return
        ctx.user_data.pop("pending_import", None)
        fields = pend["fields"]
        await query.edit_message_text(
            f"{len(fields)} ta dala yaratilmoqda…")

        def _do():
            return seed_boundaries(
                LEDGER, fields, owner_chat=chat,
                crop_key=pend["crop"], soil_key=pend["soil"],
                irrigation_method=value,
                planting_iso=pend["planting"].isoformat(),
                make_id=lambda: _next_field_id(chat),
                elevation_for=fetch_elevation)

        created = await asyncio.to_thread(_do)
        total = sum(f.area_ha for f in fields)
        await query.edit_message_text(
            f"Tayyor: {len(created)} ta dala saqlandi, jami {total:.1f} ga.\n"
            f"Har biriga ertalab alohida tavsiya keladi.\n"
            f"Hozir tekshirish: “{BTN_SUV}”. Nom o'zgartirish: /nom.")


# ------------------------------------------------- имя и ро'йхат поля
#
# /nom и /ochirish — санобслуживание поля (фаза 1 карты OneSoil): имя
# правится, поле снимается с ро'йхата АРХИВОМ. DELETE в журнале
# запрещён идеологией: история рекомендаций и отметок — это KPI.
RN_TEXT = 90        # состояние диалога переименования

NAME_MAX = 40

# Столько живёт выбор поля кнопкой в /nom — ровно conversation_timeout
# диалога с одним полем, чтобы две ветки одной команды не расходились.
RENAME_TTL_SEC = 300


async def nom_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not _authorized(update):
        await _reject(update)
        return ConversationHandler.END
    chat = update.effective_chat.id
    rows = _owner_fields(chat)
    if not rows:
        await update.message.reply_text("Avval /start buyrug'ini yuboring.")
        return ConversationHandler.END
    if len(rows) == 1:
        ctx.user_data["rename_fid"] = rows[0]["field_id"]
        await update.message.reply_text(
            f"«{rows[0]['name']}» uchun yangi nom yozing:",
            reply_markup=ReplyKeyboardRemove())
        return RN_TEXT
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton(r["name"], callback_data=f"rnm:{r['field_id']}")]
         for r in rows])
    await update.message.reply_text("Qaysi dala nomini o'zgartiramiz?",
                                    reply_markup=kb)
    return ConversationHandler.END


async def rename_pick_callback(update: Update,
                               ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Выбор поля кнопкой. Живёт ВНЕ диалога: дальше фермера ловит
    текстовый шаг диалога по флагу rename_fid — сам /nom с одним полем
    идёт тем же путём."""
    query = update.callback_query
    await query.answer()
    chat = update.effective_chat.id
    fid = query.data.split(":", 1)[1]
    row = _field_row(fid)
    if row is None or row["owner_chat_id"] != chat:
        await query.edit_message_text("Dala topilmadi.")
        return
    ctx.user_data["rename_fid"] = fid
    # Вне ConversationHandler у этого шага нет своего таймаута: у пути
    # «одно поле» его даёт conversation_timeout=300, а здесь флаг жил в
    # user_data бессрочно. Фермер, выбравший поле и ушедший, следующим
    # свободным текстом переименовывал поле в свой вопрос — и вопрос не
    # доходил до Амира. Тот же срок, что и у диалога.
    ctx.user_data["rename_until"] = time.time() + RENAME_TTL_SEC
    await query.edit_message_text(f"«{row['name']}» uchun yangi nom yozing:")


async def rename_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    chat = update.effective_chat.id
    fid = ctx.user_data.get("rename_fid")
    row = _field_row(fid) if fid else None
    if row is None or row["owner_chat_id"] != chat:
        ctx.user_data.pop("rename_fid", None)
        ctx.user_data.pop("rename_until", None)
        await update.message.reply_text("Qaytadan /nom ni bosing.",
                                        reply_markup=_menu(chat))
        return ConversationHandler.END
    name = update.message.text.strip()
    if not 1 <= len(name) <= NAME_MAX:
        await update.message.reply_text(
            f"Nom 1–{NAME_MAX} belgidan iborat bo'lsin.")
        return RN_TEXT
    ctx.user_data.pop("rename_fid", None)
    ctx.user_data.pop("rename_until", None)
    LEDGER.rename_field(fid, name)
    # Кэш карточки поля держит старое имя до десяти минут.
    ctx.user_data.get("fs_cache", {}).pop(fid, None)
    await update.message.reply_text(f"Yangi nomi: «{name}».",
                                    reply_markup=_menu(chat))
    return ConversationHandler.END


async def rename_text_free(update: Update,
                           ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    """Текст после выбора поля кнопкой (вне диалога): тот же шаг, но
    возвращает, съеден ли текст, — иначе он ушёл бы в savol."""
    if not ctx.user_data.get("rename_fid"):
        return False
    if ctx.user_data.get("rename_until", 0) < time.time():
        # Срок вышел: это уже не имя поля, а обычный вопрос — пусть
        # уходит в savol, как и любой другой текст.
        ctx.user_data.pop("rename_fid", None)
        ctx.user_data.pop("rename_until", None)
        return False
    await rename_text(update, ctx)
    return True


async def ochirish(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await _reject(update)
        return
    chat = update.effective_chat.id
    rows = _owner_fields(chat)
    if not rows:
        await update.message.reply_text("Avval /start buyrug'ini yuboring.")
        return
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton(f"🗑 {r['name']}",
                               callback_data=f"arx:{r['field_id']}")]
         for r in rows])
    await update.message.reply_text(
        "Qaysi dalani ro'yxatdan olamiz? Tarix jurnalda saqlanadi.",
        reply_markup=kb)


async def archive_callback(update: Update,
                           ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat = update.effective_chat.id
    action, fid = query.data.split(":", 1)
    if action == "arxno":
        await query.edit_message_text("Bekor qilindi.")
        return
    row = _field_row(fid)
    if row is None or row["owner_chat_id"] != chat:
        await query.edit_message_text("Dala topilmadi.")
        return
    if action == "arx":
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("Ha, ro'yxatdan ol",
                                 callback_data=f"arxok:{fid}"),
            InlineKeyboardButton("Yo'q", callback_data=f"arxno:{fid}"),
        ]])
        await query.edit_message_text(
            f"«{row['name']}» ro'yxatdan olinadimi?\n"
            "Sug'orish tarixi jurnalda saqlanadi, ertalabki xabarlar "
            "bu dala uchun to'xtaydi.", reply_markup=kb)
        return
    if action == "arxok":
        if LEDGER.archive_field(fid):
            ctx.user_data.get("fs_cache", {}).pop(fid, None)
            await query.edit_message_text(
                f"«{row['name']}» ro'yxatdan olindi. Tarix saqlanadi.")
        else:
            await query.edit_message_text("Bu dala allaqachon olib tashlangan.")


# ---------------------------------------------------------------- bajardim

def _recent_rec_ids(chat_id: int) -> dict[str, int]:
    """
    Свежие рекомендации по полям владельца — из базы, не из памяти.

    ctx.user_data живёт до первого рестарта процесса; фермер, нажавший
    «Suv berdim» после передеплоя, не должен слышать «сначала /suv»,
    если рекомендация утром уходила.
    """
    import sqlite3
    cutoff = (today_tashkent() - timedelta(days=REC_MAX_AGE_DAYS)).isoformat()
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
    # Кэш карточки у ОСТАЛЬНЫХ (наблюдатель) сбрасывается меткой в
    # bot_data — user_data чужого чата отсюда не достать.
    ctx.application.bot_data.setdefault("fs_dirty", {})[field_id] = \
        time.monotonic()
    row = _field_row(field_id)
    name = row["name"] if row else field_id
    # Двойной тап по кнопке на медленном интернете — два callback'а.
    # Второй INSERT задваивал metered, и /tejaldi занижал экономию на
    # весь объём лишней строки.
    if LEDGER.has_action(rid):
        return f"{name}: allaqachon yozib olingan."
    m3 = None
    if hours is not None and row and row["pump_m3_per_hour"]:
        m3 = hours * row["pump_m3_per_hour"]
    actual = today_tashkent() - timedelta(days=days_ago)
    # Один день — один полив. Утренний пуш и дневная «Suv holati» дают
    # две рекомендации с разными id, и проверка по id выше пропускала
    # вторую отметку за тот же полив: расход задваивался.
    if LEDGER.has_action_on_day(field_id, actual):
        return f"{name}: bu kun uchun allaqachon yozib olingan."
    LEDGER.log_action(rid, followed=True, actual_day=actual,
                      actual_m3=m3, source="farmer",
                      note=f"{hours:g} soat" if hours is not None else None)
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
    """(rec, pump, anchored, degraded, forecast) по полю, с TTL на чат.

    Прогноз для секции погоды берётся отдельным быстрым запросом, а не
    протаскивается через _compute_rec: общий путь /suv и автопуша
    трогать ради новой секции нельзя.
    """
    cache = ctx.user_data.setdefault("fs_cache", {})
    hit = cache.get(row["field_id"])
    # Метка «поле изменилось» лежит в bot_data, потому что user_data —
    # личный: отметка полива Фарруха сбрасывала кэш только ему, и
    # карточка наблюдателя ещё десять минут советовала полить поле,
    # которое сам же показывала политым сегодня.
    dirty = (getattr(ctx, "application", None)
             and ctx.application.bot_data.get("fs_dirty", {})
                 .get(row["field_id"], 0.0)) or 0.0
    if hit and time.monotonic() - hit[0] < FS_CACHE_TTL_S and hit[0] >= dirty:
        return hit[1]
    rec, pump, anchored, degraded = _compute_rec(row, today_tashkent())
    try:
        forecast = fetch_forecast(row["lat"], row["lon"], days=3)
    except Exception as exc:  # noqa: BLE001 — погода не роняет карточку
        log.warning("dala: прогноз для %s не пришёл: %s",
                    row["field_id"], exc)
        forecast = []
    # Почасовой ряд — для секции «Опрыскивание». None (а не []) при
    # сбое: build_section по None просто не выводит секцию — заглушка
    # хуже молчания.
    try:
        hourly = fetch_hourly(row["lat"], row["lon"], hours=48)
    except Exception as exc:  # noqa: BLE001
        log.warning("dala: почасовой прогноз для %s не пришёл: %s",
                    row["field_id"], exc)
        hourly = None
    data = (rec, pump, anchored, degraded, forecast, hourly)
    cache[row["field_id"]] = (time.monotonic(), data)
    return data


# Замер равномерности живёт в базе (собирает scripts/build_uniformity.py):
# карточка его только читает. Старше 90 дней — не показываем: канал
# могли почистить, привычку полива поменять.
UNIFORMITY_TTL_DAYS = 90


def _uniformity_payload(row) -> dict | None:
    if "uniformity_json" not in row.keys() or not row["uniformity_json"]:
        return None
    import json
    try:
        payload = json.loads(row["uniformity_json"])
        built = date.fromisoformat(payload.get("built", ""))
    except (ValueError, TypeError):
        return None
    if (today_tashkent() - built).days > UNIFORMITY_TTL_DAYS:
        return None
    return payload


def _fs_sections(row, ctx, lang: str) -> list:
    rec, pump, _anchored, degraded, forecast, hourly = _fs_data(row, ctx)
    last_irr = _last_irrigation(row["field_id"],
                                row["last_irrigation_date"])
    try:
        season_m3 = LEDGER.savings(row["field_id"]).metered_m3
    except KeyError:
        season_m3 = 0.0
    return assemble(
        water_section(rec, last_irr, today_tashkent(), pump, lang,
                      degraded=degraded),
        uniformity_section(row["irrigation_method"], row["area_ha"],
                           has_reach=False, lang=lang,
                           declared_ha=row["hectares"],
                           inlet_side=_inlet_side(row, lang),
                           reach=_uniformity_payload(row)),
        photo_section(row["area_ha"], row["irrigation_method"],
                      photo=_photo_verdict(row, today_tashkent()), lang=lang),
        weather_section(forecast, lang),
        spray_build_section(hourly, today_tashkent(), lang),
        cost_section(season_m3, pump, lang),
    )


def _crop_name(row, lang: str) -> str:
    crop = CROPS.get(row["crop_key"])
    if crop is None:
        return row["crop_key"]
    return crop.name_uz if lang == "uz" else crop.name_ru


def _may_setup(chat: int, row) -> bool:
    """Кто вправе настраивать поле: обводить контур, указывать сторону
    входа воды, смотреть карту.

    Хозяин — очевидно. Но и наблюдатель: обвести межу это НАСТРОЙКА
    поля, а не действие фермера. Границу участка знает агроном, он же
    сидит за картой; фермер в это время стоит в поле и обходить его
    углами ради того, что рисуется пальцем за полминуты, не должен.

    Отметка полива под это правило не попадает и остаётся у хозяина:
    полил тот, кто полил, и расписаться за него в KPI-журнале нельзя.
    """
    return row["owner_chat_id"] == chat or chat in _OBSERVERS


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
    actions: list[InlineKeyboardButton] = []
    if _may_setup(chat, row):
        # Кнопки настройки поля — контур, сторона входа, карта.
        for s in sections:
            if s.action is not None:
                actions.append(InlineKeyboardButton(
                    s.action.label, callback_data=f"{s.action.callback}:{fid}"))
    if owner:
        # А вот отметка полива — только хозяину: полил тот, кто полил,
        # и наблюдатель не вправе расписаться за него в KPI-журнале.
        label = "🚿 Bugun sug'ordim" if lang == "uz" else "🚿 Полил сегодня"
        actions.append(InlineKeyboardButton(label,
                                            callback_data=f"fs:baj:{fid}"))
    if actions:
        kb.append(actions)
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
    if not _field_status_open(chat):
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
    if not _authorized(update) or not _field_status_open(chat):
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

    if verb == "draw" and row is not None:
        if not _may_setup(chat, row):
            await query.answer("Bu dala sizniki emas.", show_alert=True)
            return
        await query.answer()
        await _draw_start(update, ctx, row)
        return

    if verb == "inlet" and row is not None:
        if not _may_setup(chat, row):
            await query.answer("Bu dala sizniki emas.", show_alert=True)
            return
        await query.answer()
        await _inlet_ask(query, row, lang)
        return

    # Подделанный или устаревший callback — молча гасим «часики».
    await query.answer()


# ---------------------------------------------------------------- контур поля
#
# ТЗ FieldStatus §2, день 2. Два пути к контуру: Mini App (нужен
# HTTPS-домен) и обход углов с геолокацией (не нужно ничего). Второй —
# основной для пилота, потому что работает уже сегодня.
#
# Геометрия и проверки живут в suv/field_shape.py; здесь только диалог.

# Адрес страницы рисования. Пусто — кнопки карты нет, остаётся обход
# углов. Telegram не откроет веб-вид по http, поэтому только https.
MINIAPP_URL = os.environ.get("MINIAPP_URL", "").strip()

BTN_CORNER = "📍 Burchakni yuborish"
BTN_DRAW_DONE = "✅ Tayyor"
BTN_DRAW_CANCEL = "❌ Bekor qilish"
BTN_DRAW_UNDO = "↩️ Oxirgisini o'chirish"
BTN_DRAW_MAP = "🗺 Xaritada chizish"

# Обход поля занимает минуты, а не часы. Забытый режим обводки не должен
# однажды подхватить геолокацию, отправленную совсем по другому поводу.
DRAW_TTL_S = 3 * 3600.0

SHAPE_ERRORS_UZ = {
    "too_few": "Kamida 3 ta burchak kerak.",
    "too_many": f"{MAX_VERTICES} tadan ko'p burchak bo'lmaydi.",
    "duplicate_point": "Bu burchak allaqachon belgilangan. "
                       "Keyingi burchakka o'tib yuboring.",
    "self_intersects": "Chegara o'zi bilan kesishdi. "
                       "Burchaklarni dala bo'ylab ketma-ket belgilang.",
    # Отдельно от самопересечения: тут контур не пересёкся, а собрать
    # его из этих точек однозначно нельзя. Догадываться мы не будем.
    "order_unclear": "Burchaklar tartibi tushunarsiz bo'ldi — dala shakli "
                     "bir nechta xil chiqishi mumkin.\nDala bo'ylab yurib, "
                     "burchaklarni KETMA-KET qaytadan belgilang.",
    "too_far": "Bu nuqtalar dalangizdan juda uzoqda. Dalada turib yuboring.",
    "too_small": "Juda kichik maydon chiqdi — burchaklarni tekshiring.",
    "too_big": "Juda katta maydon chiqdi — burchaklarni tekshiring.",
}

DRAW_LOST_UZ = ("Chegara chizish bekor bo'lgan (bot qayta ishga tushgan "
                "bo'lishi mumkin).\n"
                f"“{BTN_DALA}” → dala → “{DRAW_ACTION_UZ}” bilan qaytadan "
                "boshlang.")


def _ha1(value: float) -> str:
    """Гектары с одним знаком. Больше — ложная точность: телефонный GPS
    ставит угол с ошибкой в метры, и третий знак после запятой (10 м²)
    сообщает о точности, которой нет. Фермер подтверждает одно число и
    в карточке обязан видеть его же."""
    return f"{value:.1f}".replace(".", ",")


def _draw_keyboard(row) -> ReplyKeyboardMarkup:
    """Клавиатура режима обводки. Кнопка карты появляется, только если
    домен задан: web_app без https Telegram не откроет, а мёртвая
    кнопка хуже отсутствующей."""
    rows = [[KeyboardButton(BTN_CORNER, request_location=True)]]
    if MINIAPP_URL:
        from telegram import WebAppInfo
        url = (f"{MINIAPP_URL}?field={row['field_id']}"
               f"&lat={row['lat']:.6f}&lon={row['lon']:.6f}")
        rows.insert(0, [KeyboardButton(BTN_DRAW_MAP, web_app=WebAppInfo(url))])
    rows.append([KeyboardButton(BTN_DRAW_DONE), KeyboardButton(BTN_DRAW_UNDO)])
    rows.append([KeyboardButton(BTN_DRAW_CANCEL)])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def _draw_state(ctx: ContextTypes.DEFAULT_TYPE) -> dict | None:
    """Текущая обводка, если она ещё жива. Протухшую забываем сами."""
    draw = ctx.user_data.get("draw")
    if not draw:
        return None
    if time.monotonic() - draw.get("started", 0.0) > DRAW_TTL_S:
        ctx.user_data.pop("draw", None)
        return None
    return draw


async def _draw_lost(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопка обводки нажата, а режима нет.

    Клавиатура живёт на телефоне фермера и переживает перезапуск бота, а
    состояние диалога — нет. Молчать в этом месте нельзя: тишина читается
    как «бот умер». Возвращаем меню и говорим, что делать.
    """
    await update.effective_message.reply_text(
        DRAW_LOST_UZ, reply_markup=_menu(update.effective_chat.id))


async def _draw_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                      row) -> None:
    """Включить режим обводки. Уже собранные углы этого поля сохраняем:
    карточка с кнопкой висит в чате, и повторное нажатие (например,
    чтобы перечитать инструкцию, или после того как утренний пуш заменил
    клавиатуру) не должно стирать половину обойдённого поля."""
    chat = update.effective_chat.id
    fid = row["field_id"]
    draw = _draw_state(ctx)
    resumed = 0
    if draw and draw["fid"] == fid:
        resumed = len(draw["pts"])
        draw["started"] = time.monotonic()
    else:
        if draw and draw["pts"]:
            other = _field_row(draw["fid"])
            await ctx.bot.send_message(
                chat, f"“{other['name'] if other else draw['fid']}” "
                      f"bo'yicha {len(draw['pts'])} ta burchak saqlanmadi.")
        ctx.user_data["draw"] = {"fid": fid, "pts": [],
                                 "started": time.monotonic()}
    # Незавершённое подтверждение по другому поводу больше не действует.
    ctx.user_data.pop("draw_pending", None)

    if resumed:
        text = (f"“{row['name']}” bo'yicha {resumed} ta burchak bor.\n"
                f"Davom eting yoki “{BTN_DRAW_DONE}” ni bosing.")
    else:
        text = ("Dala chegarasini belgilaymiz.\n\n"
                "Dalaning burchaklariga borib, har birida joylashuvni yubor. "
                "Kamida 3 ta burchak kerak.\n\n"
                "Muhim: burchaklarni dala bo'ylab KETMA-KET belgilang — "
                "qanday yursangiz, dala shakli shunday chiqadi.\n"
                f"Tugagach “{BTN_DRAW_DONE}” ni bos.")
        if MINIAPP_URL:
            text += (f"\n\nDalada emasmisan? “{BTN_DRAW_MAP}” bilan "
                     f"xaritada barmoq bilan chizsang ham bo'ladi.")
    await ctx.bot.send_message(chat, text, reply_markup=_draw_keyboard(row))


def _draw_anchor(fid: str) -> tuple[float, float] | None:
    row = _field_row(fid)
    return (row["lat"], row["lon"]) if row else None


async def _draw_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                        fid: str, check, source: str) -> None:
    """Показать посчитанную площадь и спросить подтверждение.

    Площадь фермер знает по документам и ошибку заметит сразу — это
    единственная проверка того, что обведено именно его поле, и стоит
    она одного вопроса.

    Метка (nonce) в callback_data привязывает кнопку к КОНКРЕТНОМУ
    обмеру: без неё «✅ To'g'ri», нажатое на старом сообщении, сохранило
    бы новый, ещё не подтверждённый контур.
    """
    token = f"{int(time.monotonic() * 1000) % 1_000_000:06d}"
    ctx.user_data["draw_pending"] = {
        "fid": fid, "pts": [list(p) for p in check.points],
        "source": source, "token": token, "area": check.area}
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ To'g'ri",
                             callback_data=f"fsdraw:ok:{token}"),
        InlineKeyboardButton("❌ Noto'g'ri",
                             callback_data=f"fsdraw:no:{token}"),
    ]])
    await update.effective_message.reply_text(
        f"Dalangiz {_ha1(check.area)} gektar chiqdi. To'g'rimi?",
        reply_markup=kb)


async def draw_location(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Геолокация угла. Вне режима обводки — точка для свежей заметки,
    если она есть; иначе молчим, как и раньше."""
    if not _authorized(update):
        return
    draw = _draw_state(ctx)
    if not draw or not _field_status_open(update.effective_chat.id):
        if not draw:
            await _maybe_attach_note_location(update, ctx)
        return
    # effective_message, а не message: живая геолокация приходит
    # обновлениями edited_message, где update.message пустой.
    msg = update.effective_message
    loc = getattr(msg, "location", None)
    if loc is None:
        return
    if len(draw["pts"]) >= MAX_VERTICES:
        await msg.reply_text(
            f"{MAX_VERTICES} ta burchak — eng ko'pi. "
            f"“{BTN_DRAW_DONE}” ni bosing yoki “{BTN_DRAW_UNDO}” bilan "
            f"ortiqchasini o'chiring.")
        return
    draw["pts"].append((loc.latitude, loc.longitude))
    draw["started"] = time.monotonic()
    n = len(draw["pts"])
    left = max(0, 3 - n)
    tail = (f" Yana kamida {left} ta kerak." if left
            else f" “{BTN_DRAW_DONE}” ni bosishing mumkin.")
    await msg.reply_text(f"{n}-burchak qabul qilindi.{tail}")


async def draw_undo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Убрать последний угол. Без этого одна ошибка на восьмом углу
    означает заново обойти всё поле."""
    if not _authorized(update):
        return
    draw = _draw_state(ctx)
    if not draw:
        await _draw_lost(update, ctx)
        return
    if not draw["pts"]:
        await update.effective_message.reply_text("Hali burchak yo'q.")
        return
    draw["pts"].pop()
    n = len(draw["pts"])
    await update.effective_message.reply_text(
        f"O'chirdim. Hozir {n} ta burchak bor." if n
        else "O'chirdim. Burchaklar qolmadi.")


async def draw_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    draw = _draw_state(ctx)
    if not draw:
        await _draw_lost(update, ctx)
        return
    fid = draw["fid"]
    # Углы приходят в порядке обхода — это данные, а не шум. Развернуть
    # их бот вправе только когда ответ единственный (см. field_shape).
    check = validate_shape(draw["pts"], _draw_anchor(fid), reorder=True)
    if not check.ok:
        await update.effective_message.reply_text(
            SHAPE_ERRORS_UZ.get(check.error, "Chegara chiqmadi.")
            + f"\n\nHozircha {len(draw['pts'])} ta burchak bor. "
              f"“{BTN_DRAW_UNDO}” yoki “{BTN_DRAW_CANCEL}”.")
        return
    ctx.user_data.pop("draw", None)
    await update.effective_message.reply_text(
        "Hisobladim.", reply_markup=_menu(update.effective_chat.id))
    await _draw_confirm(update, ctx, fid, check, "pins")


async def draw_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    had = ctx.user_data.pop("draw", None)
    ctx.user_data.pop("draw_pending", None)
    await update.effective_message.reply_text(
        "Bekor qilindi." if had else DRAW_LOST_UZ,
        reply_markup=_menu(update.effective_chat.id))


async def draw_webapp(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Контур из Mini App: фермер обвёл поле пальцем на карте."""
    if not _authorized(update):
        return
    chat = update.effective_chat.id
    if not _field_status_open(chat):
        return
    msg = update.effective_message
    draw = _draw_state(ctx) or {}
    try:
        import json
        data = json.loads(msg.web_app_data.data)
        pts = [(float(a), float(b)) for a, b in data["polygon"]]
        fid = str(data.get("field") or draw.get("fid") or "")
    except Exception as exc:  # noqa: BLE001 — данные приходят от клиента
        log.warning("draw: web_app_data не разобрались: %s", exc)
        await msg.reply_text("Chegara kelmadi. Qayta urinib ko'ring.")
        return

    row = _field_row(fid)
    if row is None or not _may_setup(chat, row):
        await msg.reply_text("Dala topilmadi.", reply_markup=_menu(chat))
        ctx.user_data.pop("draw", None)
        return

    # Нарисованный контур НЕ пересортировываем: порядок вершин там
    # осмысленный, и «бабочку» надо показать фермеру, а не выпрямить.
    check = validate_shape(pts, (row["lat"], row["lon"]), reorder=False)
    if not check.ok:
        # Режим обводки НЕ выключаем: кнопка карты остаётся под рукой,
        # иначе «нарисуйте заново» некуда нажать.
        await msg.reply_text(
            SHAPE_ERRORS_UZ.get(check.error, "Chegara chiqmadi.")
            + f"\n\n“{BTN_DRAW_MAP}” bilan qaytadan chizib ko'ring.",
            reply_markup=_draw_keyboard(row))
        return
    ctx.user_data.pop("draw", None)
    await msg.reply_text("Qabul qilindi.", reply_markup=_menu(chat))
    await _draw_confirm(update, ctx, fid, check, "miniapp")


# ------------------------------------------------------------ фото поля
#
# ТЗ FieldStatus §4, дни 4-5. Отличие от ТЗ и причина — в suv/field_photo.py:
# красим ИЗМЕРЕННУЮ влажность, а не смоделированную заливку по борозде.
#
# Сеть и сборка картинки живут в suv/scene.py и suv/photo_render.py;
# здесь — гейт, кэш и отправка.


def _photo_verdict(row, today: date):
    """Можно ли показать фото по этому полю — по тому, что видно локально.

    Свежесть кадра здесь НЕ проверяется, и это осознанно. Раньше сюда
    подставлялась дата последнего собранного фото, и через две недели
    после просмотра кнопка исчезала навсегда с надписью «свежего снимка
    нет» — хотя спутник с тех пор проходил над полем трижды, а никто его
    не спрашивал. Ходить в сеть при каждом открытии карточки тоже нельзя.
    Поэтому здесь только то, что известно без спутника, а настоящую
    свежесть проверяет сборка фото и честно отвечает, если кадра нет.
    """
    return can_show_photo(
        area_ha=row["area_ha"], irrigation_method=row["irrigation_method"],
        has_polygon=bool(_field_polygon(row)),
        scene_day=today, today=today, valid_fraction=None)


def _latest_scene_day(row) -> str:
    """Дата самого свежего прохода спутника, '' если спросить не вышло.

    Пустая строка выключает кэш и заставляет пересобрать фото — лишняя
    работа, но не ложь: показать старую картинку под видом свежей хуже.
    """
    from suv import scene
    from suv.field_photo import bbox_of
    ring = _field_polygon(row)
    if not ring:
        return ""
    try:
        days = scene.candidate_days(scene.get_token(), bbox_of(ring))
    except Exception as exc:  # noqa: BLE001
        log.warning("дата снимка для %s не получена: %s", row["field_id"], exc)
        return ""
    return days[0].isoformat() if days else ""


# Сколько кадров подряд пробуем, прежде чем сдаться. Спутник ходит над
# Узбекистаном раз в пять дней, так что четыре попытки покрывают три
# недели — дальше кадр всё равно старше порога свежести.
PHOTO_MAX_TRIES = 4


def _build_photo(row, lang: str):
    """Собрать фото поля. Синхронная и медленная — только из потока.

    Кадр выбирается от свежего к старому: берём первый, у которого доля
    чистых пикселей ВНУТРИ контура достаточна. Фильтровать по облачности
    сцены нельзя — облако за сорок километров от поля выбраковало бы
    годный кадр (ТЗ §4.2).
    """
    import io

    from suv import photo_render, scene
    from suv.field_photo import (MIN_VALID_FRACTION, bbox_of, can_show_photo)

    today = today_tashkent()
    ring = _field_polygon(row)
    box = bbox_of(ring)
    token = scene.get_token()

    def gate(day, valid_fraction):
        return can_show_photo(
            area_ha=row["area_ha"],
            irrigation_method=row["irrigation_method"], has_polygon=True,
            scene_day=day, today=today, valid_fraction=valid_fraction)

    days = scene.candidate_days(token, box)
    if not days:
        return None, gate(None, None), "", "", ""
    latest_seen = days[0].isoformat()

    last = gate(days[0], None)
    for day in days[:PHOTO_MAX_TRIES]:
        v = gate(day, None)
        if not v.ok:
            # Уже добытый ИЗМЕРЕННЫЙ вердикт (например «поле в облаках»
            # по свежему кадру) не затираем возрастом более старого дня:
            # фермер получал «последний снимок слишком старый», хотя
            # трёхдневный снимок есть — просто поле под облаком.
            if last.ok:
                last = v
            break  # дальше только старее — искать смысла нет
        sc = scene.fetch(box, day, token)
        stats = photo_render.measure(sc, ring)
        # stats=None — внутри контура не осталось ни одного чистого
        # пикселя. Показать такой кадр значило бы прислать фермеру
        # фотографию облака вместо его поля.
        frac = stats.valid_fraction if stats else 0.0
        last = gate(day, frac)
        if not last.ok:
            continue
        # Чёткий архивный фон — лучше, но не обязателен: без него фото
        # выходит на кадре Sentinel-2, как раньше.
        try:
            from suv import basemap as bm
            base_img = bm.fetch(box)
        except Exception as exc:  # noqa: BLE001
            log.warning("подложка для %s не скачалась: %s",
                        row["field_id"], exc)
            base_img = None
        img, caption, _st = photo_render.build(
            sc, ring, field_name=row["name"], area_ha=row["area_ha"],
            lang=lang, basemap=base_img)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90)
        buf.seek(0)
        return (buf, last, day.isoformat(),
                f"🌾 {row['name']}\n\n{caption}", latest_seen)

    if last.ok:  # все попытки вышли, но причина не названа
        last = gate(days[0], 0.0)
    return None, last, "", "", latest_seen


async def photo_callback(update: Update,
                         ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat = update.effective_chat.id
    if not _authorized(update) or not _field_status_open(chat):
        await query.answer()
        return
    parts = query.data.split(":", 2)
    fid = parts[2] if len(parts) > 2 else ""
    row = _field_row(fid)
    rows, observer = _fields_for_view(chat)
    if row is None or fid not in {r["field_id"] for r in rows}:
        await query.answer()
        return
    lang = "ru" if observer else "uz"

    # Отвечаем ДО похода в сеть: Telegram ждёт ответа на колбэк секунды,
    # а спутник отвечает десятками. Иначе «часики» на кнопке висят до
    # таймаута, и фермер жмёт её снова.
    await query.answer("Surat tayyorlanmoqda…" if lang == "uz"
                       else "Готовлю снимок…")
    await ctx.bot.send_chat_action(chat, ChatAction.UPLOAD_PHOTO)

    # Ключ кэша: версия отрисовки + язык + контур. Версия обязательна —
    # без неё выкатка нового вида фото ничего не меняет для фермера,
    # пока спутник не пройдёт заново. Язык обязателен тоже: без него
    # подпись навсегда оставалась на языке первого нажавшего — Фаррух
    # получал снимок своего поля с русской подписью наблюдателя.
    from suv.photo_render import STYLE_VERSION, when_word
    key = f"v{STYLE_VERSION}:{lang}:{_contour_fingerprint(row)}"
    # Дату свежего кадра спрашиваем у каталога. Сравнивать сохранённую
    # дату саму с собой бессмысленно: такой «кэш» никогда не заметил бы
    # нового снимка и отдавал бы августовскую картинку до конца сезона.
    latest = await asyncio.to_thread(_latest_scene_day, row)
    cached = LEDGER.cached_photo(fid, latest, key) if latest else None
    if cached:
        # Повторное нажатие отдаёт готовый file_id мгновенно (ТЗ §4.5).
        # Подпись обязательна и здесь: фото без даты снимка фермер
        # прочитает как сегодняшнее (§1.2). Относительное слово при
        # этом пересчитывается: сохранённое «(kecha)» через три дня —
        # неправда, а читают его раньше, чем цифры даты.
        file_id, caption, scene_iso = cached
        if caption and scene_iso:
            word = when_word(date.fromisoformat(scene_iso), today_tashkent(),
                             lang == "uz")
            caption = re.sub(r"(🛰[^:\n]+: \d{2}\.\d{2}) \([^)]*\)",
                             rf"\1 ({word})", caption, count=1)
        await ctx.bot.send_photo(chat, file_id, caption=caption or None)
        return

    try:
        buf, verdict, scene_day, caption, latest_seen = \
            await asyncio.to_thread(_build_photo, row, lang)
    except Exception as exc:  # noqa: BLE001 — сеть и Copernicus падают
        log.warning("фото %s не собралось: %s", fid, exc)
        await ctx.bot.send_message(
            chat, "Surat olinmadi. Birozdan keyin qayta urinib ko'ring."
            if lang == "uz" else "Снимок не получился. Попробуйте позже.")
        return

    if buf is None:
        why = PHOTO_BLOCKED.get(verdict.reason)
        text = (why[0] if lang == "uz" else why[1]) if why else (
            "Surat chiqmadi." if lang == "uz" else "Снимок не получился.")
        await ctx.bot.send_message(chat, text)
        return

    msg = await ctx.bot.send_photo(chat, buf, caption=caption)
    if msg and msg.photo:
        # latest_seen — дата каталога на момент сборки. Без неё кэш не
        # срабатывал НИКОГДА, пока свежий проход облачный: фото законно
        # собрано по старому чистому дню, дата не равна каталожной, и
        # каждое нажатие шло в 30-60-секундную пересборку той же
        # картинки, сжигая квоту Copernicus.
        LEDGER.save_photo(fid, msg.photo[-1].file_id, scene_day, key,
                          caption, latest_seen=latest_seen or scene_day)
        ctx.user_data.get("fs_cache", {}).pop(fid, None)


# --------------------------------------------------- сторона входа воды
#
# ТЗ §2.3, день 3. Короткий шаг после контура: без него не построить ось
# борозды, а значит и заливку. Храним индексами вершин, а не румбом:
# «с севера» на вытянутом поле может означать два разных ребра.

# Больше восьми кнопок — это уже не выбор, а список. Длинные рёбра идут
# первыми: вода заходит с канала, а он тянется вдоль длинной межи.
INLET_MAX_CHOICES = 8


def _contour_fingerprint(row) -> str:
    """Короткая метка текущего контура.

    Индексы вершин сами по себе от устаревшей карточки не защищают: пара
    (0,1) есть у ЛЮБОГО контура. Фермер, перечертивший поле и нажавший
    кнопку на старом сообщении, записал бы совсем другую межу — с той же
    уверенной формулировкой «записал: вода заходит с севера».
    """
    import hashlib
    raw = row["polygon_geojson"] if "polygon_geojson" in row.keys() else None
    if not raw:
        return "0"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:6]


def _inlet_choices(row, lang: str):
    """Откуда может заходить вода: стороны поля и ворота между ними.

    Ворота — короткий торец, который склейка убирает внутрь длинной
    межи. Без них список молча терял единственный верный ответ: на
    винограднике Фарруха вода заходит с северо-востока через межу в
    33 метра, и северо-востока в кнопках не было вовсе.
    """
    ring = _field_polygon(row)
    if not ring:
        return [], 0
    all_sides = polygon_inlet_options(from_geojson_ring(ring), lang)
    return all_sides[:INLET_MAX_CHOICES], len(all_sides)


def _inlet_side(row, lang: str) -> str | None:
    """Название стороны входа для карточки, или None если не выбрана.

    Эта функция на общем пути карточки: любой мусор в колонке не должен
    ронять весь экран, поэтому ловим не только ошибку разбора JSON, но и
    всё, во что он может развернуться.
    """
    raw = row["inlet_vertices"] if "inlet_vertices" in row.keys() else None
    ring = _field_polygon(row)
    if not raw or not ring:
        return None
    import json
    try:
        i, j = (int(v) for v in json.loads(raw))
    except Exception:  # noqa: BLE001 — колонку мог испортить кто угодно
        log.warning("сторона входа %s не читается: %r", row["field_id"], raw)
        return None
    edge = polygon_inlet_edge(from_geojson_ring(ring), (i, j))
    return edge.name(lang) if edge else None


async def _inlet_ask(query, row, lang: str) -> None:
    fid = row["field_id"]
    choices, total = _inlet_choices(row, lang)
    if not choices:
        await _fs_edit(query, "Avval dala chegarasini chizing." if lang == "uz"
                       else "Сначала обведите поле.",
                       InlineKeyboardMarkup([[InlineKeyboardButton(
                           "⬅️ Orqaga" if lang == "uz" else "⬅️ Назад",
                           callback_data=f"fs:card:{fid}")]]))
        return
    fp = _contour_fingerprint(row)
    kb = [[InlineKeyboardButton(
        label, callback_data=f"fsin:{fid}:{e.i}:{e.j}:{fp}")]
        for e, label in choices]
    # Если в списке нет той межи, с которой заходит вода, единственный
    # честный выход — перечертить контур: значит, он обведён неточно.
    kb.append([InlineKeyboardButton(
        "🗺 Qayta chizish" if lang == "uz" else "🗺 Обвести заново",
        callback_data=f"fs:draw:{fid}")])
    kb.append([InlineKeyboardButton(
        "⬅️ Orqaga" if lang == "uz" else "⬅️ Назад",
        callback_data=f"fs:card:{fid}")])
    text = ("Suv dalaga qaysi tomondan kiradi?" if lang == "uz"
            else "С какой стороны вода заходит на поле?")
    if total > len(choices):
        # Молча обрезать список нельзя: фермер решит, что его межи тут
        # нет вообще, вместо того чтобы уточнить контур.
        text += (f"\n\nEng uzun {len(choices)} tasi ko'rsatildi "
                 f"({total} tadan). Kerakli tomon yo'q bo'lsa — "
                 f"chegarani qaytadan chizing."
                 if lang == "uz" else
                 f"\n\nПоказаны {len(choices)} самых длинных из {total}. "
                 f"Нужной стороны нет — обведите контур заново.")
    await _fs_edit(query, text, InlineKeyboardMarkup(kb))


async def inlet_callback(update: Update,
                         ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat = update.effective_chat.id
    if not _authorized(update) or not _field_status_open(chat):
        await query.answer()
        return
    try:
        _, fid, raw_i, raw_j, fp = query.data.split(":", 4)
        i, j = int(raw_i), int(raw_j)
    except ValueError:
        await query.answer()
        return

    row = _field_row(fid)
    if row is None or not _may_setup(chat, row):
        await query.answer()
        return

    rows, observer = _fields_for_view(chat)
    lang = "ru" if observer else "uz"
    many = len(rows) > 1

    stale = fp != _contour_fingerprint(row)
    edge = next((e for e, _l in _inlet_choices(row, lang)[0]
                 if (e.i, e.j) == (i, j)), None)
    if stale or edge is None:
        # Контур с тех пор перечертили: показываем список заново по
        # нынешней границе, а не записываем межу, которой уже нет.
        await query.answer("Chegara o'zgargan." if lang == "uz"
                           else "Контур изменился.", show_alert=True)
        await _inlet_ask(query, row, lang)
        return

    await query.answer()
    LEDGER.save_inlet(fid, i, j)
    ctx.user_data.get("fs_cache", {}).pop(fid, None)
    ctx.application.bot_data.setdefault("fs_dirty", {})[fid] = time.monotonic()
    log.info("сторона входа воды: %s -> вершины %d,%d (%s)",
             fid, i, j, edge.name("ru"))
    # Возвращаемся на карточку: экран правит одно сообщение, и оставить
    # его без кнопок значит завести фермера в тупик.
    row = _field_row(fid)
    text, kb = await asyncio.to_thread(_fs_card, row, chat, ctx, lang, many)
    await _fs_edit(query, text, kb)


async def draw_confirm_callback(update: Update,
                                ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat = update.effective_chat.id
    if not _authorized(update) or not _field_status_open(chat):
        await query.answer()
        return
    try:
        _, verb, token = query.data.split(":", 2)
    except ValueError:
        await query.answer()
        return

    # Забираем подтверждение ДО долгих операций: на 2G фермер успевает
    # нажать «✅» дважды, и второй обработчик не должен переписать
    # сообщение об успехе отказом.
    pending = ctx.user_data.get("draw_pending")
    if not pending or pending["token"] != token:
        await query.answer()
        await query.edit_message_text(
            "Bu o'lchov eskirgan. Chegarani qaytadan belgilang.")
        return
    ctx.user_data.pop("draw_pending", None)
    await query.answer()

    if verb != "ok":
        await query.edit_message_text(
            "Yaxshi, chegarani saqlamadim. Qaytadan chizsak bo'ladi.")
        return

    fid = pending["fid"]
    pts = [(p[0], p[1]) for p in pending["pts"]]
    ring = to_geojson_ring(pts)
    ha = polygon_area_ha(pts)
    LEDGER.save_polygon(fid, ring, ha, pending["source"])
    # Карточка обязана перестать говорить «чегара чизилмаган» сразу,
    # а не через десять минут TTL, — и у наблюдателя тоже.
    ctx.user_data.get("fs_cache", {}).pop(fid, None)
    ctx.application.bot_data.setdefault("fs_dirty", {})[fid] = time.monotonic()
    log.info("контур сохранён: %s, %.2f га, источник %s",
             fid, ha, pending["source"])

    text = f"Chegara saqlandi: {_ha1(ha)} gektar."
    if os.environ.get("CDSE_CLIENT_ID") and os.environ.get("CDSE_CLIENT_SECRET"):
        # Обещать спутник без ключей нельзя: §6 требует, чтобы бот
        # работал и без Copernicus, а обещание — чтобы оно было правдой.
        text += "\nEndi sun'iy yo'ldosh dalangizni aniq chegara bo'yicha ko'radi."
    row = _field_row(fid)
    if row and row["hectares"]:
        # Две площади — заявленная и обмеренная — не должны молча
        # разойтись: расхождение и есть повод перепроверить контур.
        diff = abs(ha - row["hectares"]) / row["hectares"]
        if diff >= 0.10:
            text += (f"\n\n⚠️ Ro'yxatda {_ha1(row['hectares'])} ga edi, "
                     f"o'lchov {_ha1(ha)} ga berdi. Qaysi biri to'g'ri?")
    await query.edit_message_text(text)


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


async def daily_push(ctx: ContextTypes.DEFAULT_TYPE,
                     only_if_silent_today: bool = False) -> None:
    """Утренний обход всех владельцев полей.

    only_if_silent_today — режим догона после рестарта: слать только
    тем, у кого сегодня ещё не было ни одной рекомендации (ни пушем, ни
    кнопкой). Без этого каждый деплой в течение дня повторял бы
    владельцу уже отправленное утреннее сообщение.
    """
    today = today_tashkent()
    import sqlite3
    with sqlite3.connect(LEDGER.path) as c:
        owners = [r[0] for r in c.execute(
            "SELECT DISTINCT owner_chat_id FROM fields "
            "WHERE owner_chat_id IS NOT NULL AND archived_at IS NULL")]

    for chat in owners:
        # Ошибка на одном владельце не смеет оставить без утреннего
        # сообщения остальных: раньше исключение из любой строки ниже
        # (sqlite locked, битая строка поля) убивало весь обход, и
        # хвост списка молча оставался без пуша.
        try:
            await _push_owner(ctx, chat, today, only_if_silent_today)
        except Exception as exc:  # noqa: BLE001
            log.warning("push: владелец %s пропущен: %s", chat, exc)


async def _push_owner(ctx, chat: int, today: date,
                      only_if_silent_today: bool) -> None:
    if _ALLOWED and chat not in _ALLOWED:
        return  # поле в базе есть, а доступа у владельца больше нет
    rows = _owner_fields(chat)
    if not rows:
        return
    last_rec = _last_rec_date(chat)
    if only_if_silent_today and last_rec == today:
        return  # догон: сегодня уже что-то уходило

    recs = []
    for row in rows:
        try:
            # В поток по той же причине, что и в /suv: утренний обход с
            # медленным Copernicus замораживал бота на минуты, и догон
            # после каждого деплоя открывал это окно заново.
            recs.append(await asyncio.to_thread(_compute_rec, row, today))
        except Exception as exc:  # noqa: BLE001
            log.warning("push: расчёт %s не удался: %s",
                        row["field_id"], exc)
    if not recs:
        return

    # Деградированный расчёт в срочности УЧАСТВУЕТ. Пробовали исключать —
    # мол, он построен на нормах, а не на данных, — но так «поливай
    # сегодня» терялось ровно в тот день, когда погодный сервис лежит, а
    # поле сохнет. Промолчать тут дороже, чем предупредить лишний раз:
    # о приблизительности расчёта фермеру говорит сам текст.
    urgent = any(rec.action_day is not None and rec.days_until <= 1
                 for rec, _p, _a, _degraded in recs)
    if not push_due(urgent, last_rec, today):
        log.info("push: %s — сегодня уже слали, молчим", chat)
        return

    ud = ctx.application.user_data[chat]
    ud.setdefault("last_rec_ids", {})
    delivered = 0
    for rec, pump, anchored, degraded in recs:
        try:
            sent = await ctx.bot.send_message(
                chat, _rec_message(rec, pump, "uz", anchored=anchored,
                                   degraded=degraded),
                reply_markup=_why_markup(chat, rec.field.field_id))
        except Exception as exc:  # noqa: BLE001
            log.warning("push: отправка %s не удалась: %s", chat, exc)
            continue
        delivered += 1
        # В журнал — всё ДОСТАВЛЕННОЕ, включая расчёт по нормам: правило
        # журнала «храним то, что сказали фермеру», и на этой же записи
        # держатся кнопка «✅ Suv berdim» (rid) и защита догона от
        # дублей. Недоставленное не пишем — оно и не сказано. Ошибка
        # записи не роняет обход: завтра пуш просто повторится.
        try:
            rid = LEDGER.log_recommendation(rec, __version__)
            ud["last_rec_ids"][rec.field.field_id] = rid
        except Exception as exc:  # noqa: BLE001
            log.warning("push: журнал по %s не записан: %s",
                        rec.field.field_id, exc)
            continue
        # Кнопки оценки требуют id рекомендации, а id появляется только
        # после записи в журнал — журнал же пишет только ДОСТАВЛЕННОЕ
        # (правило «храним сказанное»). Поэтому клавиатура правится
        # задним числом; сбой правки не роняет обход.
        try:
            await sent.edit_reply_markup(
                _why_markup(chat, rec.field.field_id, rid=rid))
        except Exception:  # noqa: BLE001
            pass
    # «Отправлено» здесь однажды соврало: единственная отправка упала с
    # Forbidden (владелец заблокировал бота), continue проскочил мимо
    # журнала — а итоговая строка всё равно отчиталась об успехе, и по
    # логам утро выглядело доставленным. Считаем настоящие доставки.
    if delivered:
        log.info("push: %s — доставлено %d из %d полей, срочно=%s",
                 chat, delivered, len(recs), urgent)
    else:
        log.warning("push: %s — не доставлено ничего из %d полей",
                    chat, len(recs))


async def push_catchup(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Догнать утренний пуш, пропущенный из-за рестарта.

    Деплой перезапускает службу на каждый push в main; попади рестарт
    на 06:00 — job в памяти APScheduler исчезает вместе с процессом, и
    новый процесс молча ставил следующий запуск на завтра. Утро целиком
    выпадало, и в логах об этом не было ни строки.
    """
    from suv.clock import now as now_tashkent
    if now_tashkent().hour < PUSH_HOUR_TASHKENT:
        return  # до утреннего часа пуш ещё впереди, догонять нечего
    log.info("push: проверяю, не пропущен ли утренний обход")
    await daily_push(ctx, only_if_silent_today=True)


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


async def kabinet(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Открыть веб-кабинет. Отвечаем inline-кнопкой, а не открываем из
    reply-клавиатуры: только inline-запуск гарантированно передаёт
    странице подпись Telegram (initData) на всех клиентах — Desktop
    для кнопок нижней клавиатуры её не шлёт вовсе."""
    chat = update.effective_chat.id
    if chat not in _CABINET or not CABINET_URL:
        # Кнопка могла залипнуть в старой клавиатуре — отвечаем внятно.
        await update.message.reply_text(
            "Kabinet hozircha yopiq sinovda.", reply_markup=_menu(chat))
        return
    from telegram import WebAppInfo
    await update.message.reply_text(
        "Dala kabineti: maslahat, 14 kunlik reja, sug'orishlar va hisob "
        "tarixi — bitta ekranda.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
            "🖥 Kabinetni ochish", web_app=WebAppInfo(CABINET_URL))]]))


async def _escape_to_dala(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await dala_holati(update, ctx)
    return ConversationHandler.END


SAVOL_UZ = ("Savolingizni oldim — Amir javob beradi.\n"
            "Shoshilinch bo'lsa, tavsiyani “{btn}” tugmasi bilan oling.")
SAVOL_RU = ("Вопрос получен — Амир ответит.\n"
            "Если срочно, рекомендация всегда доступна кнопкой «{btn}».")


async def savol(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if await rename_text_free(update, ctx):
        return
    """Свободный текст вне мастера: ответить и переслать Амиру.

    До этого текст, не совпавший ни с одной кнопкой, не обрабатывался ни
    одним хендлером — фермер, написавший вопрос, получал ровно ничего.
    Это единственное место, где проект сам нарушал собственное правило
    «тишина читается как бот умер», и заодно единственный канал, из
    которого вообще можно узнать, что фермер хочет спросить.

    Никакого ИИ: бот не отвечает на вопрос, он подтверждает получение и
    отдаёт вопрос человеку. Обещать разговор, которого нет, — тот же
    обман, что и обещать функцию, которой нет.
    """
    if not _authorized(update):
        await _reject(update)
        return
    chat = update.effective_chat.id
    text = (update.message.text or "").strip()
    if not text:
        return
    log.info("вопрос от %s: %s", chat, text[:300])

    for admin in _ADMIN_CHAT:
        if admin == chat:
            continue  # Амир не пересылает сам себе
        try:
            await ctx.bot.forward_message(chat_id=admin,
                                          from_chat_id=chat,
                                          message_id=update.message.message_id)
        except Exception as exc:  # noqa: BLE001 — доставка Амиру не важнее ответа фермеру
            log.warning("вопрос не переслан в %s: %s", admin, exc)

    lang = "ru" if chat in _OBSERVERS else "uz"
    tpl = SAVOL_RU if lang == "ru" else SAVOL_UZ
    await update.message.reply_text(tpl.format(btn=BTN_SUV),
                                    reply_markup=_menu(chat))


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Фермер не должен получать тишину: тишина читается как «бот умер»."""
    log.exception("handler failed", exc_info=ctx.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "Kechirasiz, xatolik yuz berdi. Birozdan keyin qayta urinib ko'ring.")
        except Exception:  # noqa: BLE001 — сеть могла упасть целиком
            pass


async def _post_init(app: Application) -> None:
    """Синее меню команд в Telegram.

    set_my_commands не вызывался ни разу: /suv и /tejaldi существовали,
    но найти их можно было только из /yordam или по кнопке. Команда, о
    которой клиент не знает, для фермера не существует.

    /dala сюда НЕ попадает сознательно: экран живёт в закрытом демо, и
    показывать его в общем меню значило бы рекламировать всем то, что
    работает у трёх чатов.
    """
    from telegram import BotCommand
    try:
        await app.bot.set_my_commands([
            BotCommand("suv", "Bugungi sug'orish tavsiyasi"),
            BotCommand("bajardim", "Suv berganingizni belgilash"),
            BotCommand("tejaldi", "Mavsumda tejalgan suv"),
            BotCommand("yordam", "Yordam"),
            BotCommand("start", "Yangi dala qo'shish"),
            BotCommand("nom", "Dala nomini o'zgartirish"),
            BotCommand("ochirish", "Dalani ro'yxatdan olish"),
        ])
    except Exception as exc:  # noqa: BLE001 — меню не повод не стартовать
        log.warning("set_my_commands не прошёл: %s", exc)


def main() -> None:
    token = os.environ["TELEGRAM_TOKEN"]
    app = Application.builder().token(token).post_init(_post_init).build()
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
            SOIL: [MessageHandler(not_menu, got_soil)],
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
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("nom", nom_start)],
        states={RN_TEXT: [MessageHandler(not_menu, rename_text)]},
        fallbacks=[
            MessageHandler(filters.Regex(f"^{re.escape(BTN_SUV)}$"), _escape_to_suv),
            MessageHandler(filters.Regex(f"^{re.escape(BTN_TEJALDI)}$"), _escape_to_tejaldi),
            MessageHandler(filters.Regex(f"^{re.escape(BTN_BAJARDIM)}$"), _escape_to_bajardim),
            MessageHandler(filters.Regex(f"^{re.escape(BTN_YORDAM)}$"), _escape_to_yordam),
        ],
        conversation_timeout=300,
    ))
    app.add_handler(CommandHandler("ochirish", ochirish))
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
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_KABINET)}$"), kabinet))
    # Обводка контура. Все три текстовые кнопки живут только в режиме
    # рисования: их хендлеры молча выходят, если режим не включён, —
    # поэтому в MENU_FILTER они не добавляются и мастер не задевают.
    draw_only = (filters.ALL if not _FIELD_STATUS
                 else filters.Chat(_FIELD_STATUS))
    app.add_handler(MessageHandler(
        filters.Regex(f"^{re.escape(BTN_DRAW_DONE)}$") & draw_only, draw_done))
    app.add_handler(MessageHandler(
        filters.Regex(f"^{re.escape(BTN_DRAW_UNDO)}$") & draw_only, draw_undo))
    app.add_handler(MessageHandler(
        filters.Regex(f"^{re.escape(BTN_DRAW_CANCEL)}$") & draw_only, draw_cancel))
    app.add_handler(MessageHandler(
        filters.StatusUpdate.WEB_APP_DATA & draw_only, draw_webapp))
    # Локация вне мастера регистрации: ConversationHandler зарегистрирован
    # выше и в состоянии LOCATION забирает её себе, сюда доходит только
    # то, что мастеру не предназначалось.
    app.add_handler(MessageHandler(filters.LOCATION & draw_only, draw_location))
    # Фото = полевая заметка. Только демо-чаты: новая поверхность не
    # доходит до Фарруха, пока её не обкатали (правило закрытого демо).
    app.add_handler(MessageHandler(filters.PHOTO & draw_only, photo_note))
    # Файл границ = пакетный посев полей (KML/KMZ/GeoJSON/shapefile-zip).
    app.add_handler(MessageHandler(filters.Document.ALL, import_doc))
    app.add_handler(CallbackQueryHandler(why_callback, pattern=r"^why:"))
    app.add_handler(CallbackQueryHandler(feedback_callback, pattern=r"^fb:"))
    app.add_handler(CallbackQueryHandler(rename_pick_callback, pattern=r"^rnm:"))
    app.add_handler(CallbackQueryHandler(import_flow_callback,
                                         pattern=r"^imp[cmasw]:"))
    app.add_handler(CallbackQueryHandler(archive_callback, pattern=r"^arx(ok|no)?:"))
    app.add_handler(CallbackQueryHandler(note_field_callback, pattern=r"^notefld:"))
    app.add_handler(CallbackQueryHandler(bajardim_field_callback, pattern=r"^bajfld:"))
    app.add_handler(CallbackQueryHandler(bajardim_hours_callback, pattern=r"^bajardim:"))
    app.add_handler(CallbackQueryHandler(bajardim_day_callback, pattern=r"^bajday:"))
    app.add_handler(CallbackQueryHandler(draw_confirm_callback, pattern=r"^fsdraw:"))
    app.add_handler(CallbackQueryHandler(inlet_callback, pattern=r"^fsin:"))
    app.add_handler(CallbackQueryHandler(photo_callback, pattern=r"^fs:map:"))
    app.add_handler(CallbackQueryHandler(fs_callback, pattern=r"^fs:"))
    # ПОСЛЕДНИМ: всё, что не команда, не кнопка и не ответ мастеру. До
    # этого такой текст не доходил ни до кого. Кнопки перечислены явно,
    # хотя порядок регистрации и так отдаёт им приоритет: залипшая в
    # клавиатуре кнопка обводки вне режима рисования должна вести себя
    # как раньше — молча выйти, а не превратиться в вопрос Амиру.
    _handled = "|".join(re.escape(b) for b in (
        BTN_SUV, BTN_BAJARDIM, BTN_TEJALDI, BTN_YORDAM, BTN_DALA,
        BTN_KABINET, BTN_DRAW_DONE, BTN_DRAW_UNDO, BTN_DRAW_CANCEL))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND
        & ~filters.Regex(f"^({_handled})$"), savol))
    app.add_error_handler(on_error)
    if _ALLOWED:
        log.info("allowlist active: %s", sorted(_ALLOWED))
    # Обводка пальцем по карте — единственный способ обвести поле, не
    # выходя из дома. Без MINIAPP_URL кнопки нет вовсе, и снятый гейт
    # даёт фермеру только обход углов ногами: для члена жюри за столом
    # это то же самое, что функции нет. Гейт обязан называть себя.
    if MINIAPP_URL:
        log.info("Mini App обводки: %s", MINIAPP_URL)
    else:
        log.warning("MINIAPP_URL пуст — кнопки «%s» НЕТ. Контур можно "
                    "обвести только обходом углов с геолокацией.",
                    BTN_DRAW_MAP)
    if _FIELD_STATUS:
        log.info("Dala holati: закрытое демо, чаты %s", sorted(_FIELD_STATUS))
    else:
        log.info("Dala holati: ОТКРЫТ ВСЕМ (FIELD_STATUS_CHAT_IDS пуст) — "
                 "экран поля, обводка контура, снимок, заметки, "
                 "окна опрыскивания и «почему такой совет»")
    # Каждый гейт называет себя при старте — иначе из журнала не понять,
    # взведён он или нет. У запасного источника снимков это особенно
    # важно: он срабатывает только в облачный день, и без строки на
    # старте «Landsat молчит» неотличимо от «Landsat выключен».
    if landsat_enabled():
        log.info("Landsat: запасной источник NDVI включён "
                 "(идёт в дело, только когда Sentinel-2 без годного кадра)")

    if app.job_queue:
        import datetime as _dt
        from zoneinfo import ZoneInfo
        app.job_queue.run_daily(
            daily_push,
            time=_dt.time(PUSH_HOUR_TASHKENT, 0,
                          tzinfo=ZoneInfo("Asia/Tashkent")))
        # Догон на старте: рестарт (деплой идёт на каждый push в main)
        # поверх 06:00 съедал утренний обход целиком — job живёт в
        # памяти процесса и вместе с ним умирает.
        app.job_queue.run_once(push_catchup, when=15)
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
