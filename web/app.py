"""Кабинет фермера: те же цифры, что в боте, но на большом экране.

Зачем отдельная веб-версия. В чате помещается карточка — «когда и на
сколько часов». Историю за сезон, журнал поливов и разбор по секциям в
сообщение не уложить, а агроному и фермеру перед сезоном смотреть их
надо. Кабинет не заменяет бота: бот остаётся местом, где приходит совет
и отмечается полив.

Главное правило: **веб не считает ничего сам**. Все числа берутся
вызовом того же `_compute_rec` и той же сборки секций, что и утренний
пуш. Вторая реализация расчёта означала бы вторую правду, а на вопрос
«почему в боте одна дата, а на сайте другая» ответить было бы нечем.

Вход — по подписи Telegram (см. suv/webauth.py), логинов и паролей нет.
Доступ к полям решает `_fields_for_view`: та же функция, что в боте,
поэтому наблюдатель видит чужие поля ровно там же, где и в чате, а
посторонний не видит ничего.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field as dc_field
from datetime import date

from fastapi import Depends, FastAPI, Header, HTTPException, Path
from fastapi.responses import JSONResponse

from suv.webauth import AuthError, TelegramUser, verify_init_data

# Импорт бота даёт расчёт, доступ и справочники одним куском. Модуль
# импорт-безопасен: run_polling живёт под __main__.
import bot.main as B
from suv.field_status import STATUS_WORD, Status, overall_status

log = logging.getLogger("suv.web")

app = FastAPI(title="SUV AI · кабинет фермера", docs_url=None, redoc_url=None)

LANGS = {"uz", "ru", "en"}


# ── контекст-заглушка ──────────────────────────────────────────
# _fs_data кэширует расчёт в ctx.user_data и сбрасывает кэш по метке в
# ctx.application.bot_data. В вебе телеграмного контекста нет, но
# ломать общий путь ради этого нельзя — подставляем совместимую пару.
@dataclass
class _App:
    bot_data: dict = dc_field(default_factory=dict)


@dataclass
class _Ctx:
    user_data: dict = dc_field(default_factory=dict)
    application: _App = dc_field(default_factory=_App)


_CTX: dict[int, _Ctx] = {}


def _ctx_for(chat_id: int) -> _Ctx:
    """Свой кэш на пользователя — как user_data в боте."""
    return _CTX.setdefault(chat_id, _Ctx())


# ── аутентификация ─────────────────────────────────────────────
def current_user(authorization: str = Header(default="")) -> TelegramUser:
    """Кто пришёл, по подписи Telegram. Иначе 401 без подробностей."""
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    scheme, _, raw = authorization.partition(" ")
    if scheme.lower() != "tma" or not raw:
        raise HTTPException(401, "нужен заголовок Authorization: tma <initData>")
    try:
        return verify_init_data(raw, token)
    except AuthError as exc:
        # В лог — причина, наружу — общее «не пущу»: подсказывать, что
        # именно не сошлось, значит помогать подбирать.
        log.warning("вход отклонён: %s", exc)
        raise HTTPException(401, "подпись Telegram не подтвердилась") from exc


def _lang(row, user: TelegramUser) -> str:
    code = (user.language_code or "").lower()[:2]
    return code if code in LANGS else "uz"


def _fields_of(user: TelegramUser) -> tuple[list, bool]:
    return B._fields_for_view(user.id)


def _row_or_404(user: TelegramUser, field_id: str):
    rows, _observer = _fields_of(user)
    for r in rows:
        if r["field_id"] == field_id:
            return r
    # 404, а не 403: посторонний не должен узнать даже того, что поле с
    # таким идентификатором существует.
    raise HTTPException(404, "поле не найдено")


def _section_json(s, lang: str) -> dict:
    return {
        "key": s.key,
        "title": s.title,
        "status": int(s.status),
        "status_word": STATUS_WORD.get(lang, STATUS_WORD["ru"]).get(s.status, ""),
        "line": s.line,
        "hint": s.hint,
        "informational": s.informational,
    }


# ── ручки ──────────────────────────────────────────────────────
@app.get("/api/me")
def me(user: TelegramUser = Depends(current_user)):
    """Кто я и какие у меня поля — экран списка."""
    rows, observer = _fields_of(user)
    ctx = _ctx_for(user.id)
    fields = []
    for row in rows:
        lang = _lang(row, user)
        try:
            sections = B._fs_sections(row, ctx, lang)
            st = int(overall_status(sections))
        except Exception as exc:  # noqa: BLE001 — одно поле не роняет список
            log.warning("список: поле %s не посчиталось: %s",
                        row["field_id"], exc)
            st = int(Status.NO_DATA)
        fields.append({
            "field_id": row["field_id"],
            "name": row["name"],
            "hectares": row["hectares"],
            "area_ha": row["area_ha"],
            "crop": B._crop_name(row, lang),
            "status": st,
            "owner": row["owner_chat_id"] == user.id,
        })
    return {"user": {"id": user.id, "name": user.display_name},
            "observer": observer, "fields": fields}


@app.get("/api/field/{field_id}")
def field_detail(field_id: str = Path(pattern=r"^[A-Za-z0-9_-]{1,64}$"),
                 user: TelegramUser = Depends(current_user)):
    """Всё по одному полю: секции, совет, спутник, журнал."""
    row = _row_or_404(user, field_id)
    lang = _lang(row, user)
    ctx = _ctx_for(user.id)
    rec, pump, anchored, degraded, forecast = B._fs_data(row, ctx)
    sections = B._fs_sections(row, ctx, lang)
    last_irr = B._last_irrigation(row["field_id"], row["last_irrigation_date"])
    plan = rec.plan[0] if rec.plan else None

    import sqlite3
    with sqlite3.connect(B.LEDGER.path) as c:
        journal = [
            {"generated_on": g, "action_day": a, "et0_mm": e, "kc": k,
             "depletion_mm": d, "raw_mm": r, "gross_m3": m}
            for g, a, e, k, d, r, m in c.execute(
                """SELECT generated_on, action_day, et0_mm, kc, depletion_mm,
                          raw_mm, gross_m3 FROM recommendations
                   WHERE field_id=? ORDER BY id DESC LIMIT 30""",
                (field_id,))]

    try:
        sav = B.LEDGER.savings(field_id)
        metered_m3 = sav.metered_m3
    except Exception:  # noqa: BLE001 — журнал экономии не роняет карточку
        metered_m3 = 0.0

    return {
        "field": {
            "field_id": row["field_id"], "name": row["name"],
            "hectares": row["hectares"], "area_ha": row["area_ha"],
            "crop": B._crop_name(row, lang),
            "irrigation_method": row["irrigation_method"],
            "lat": row["lat"], "lon": row["lon"],
            "owner": row["owner_chat_id"] == user.id,
        },
        "overall": int(overall_status(sections)),
        "sections": [_section_json(s, lang) for s in sections],
        "advice": {
            "action_day": rec.action_day.isoformat() if rec.action_day else None,
            "days_until": rec.days_until,
            "gross_m3": round(rec.gross_m3),
            "gross_mm": round(rec.gross_mm, 1),
            "reason": rec.reason_key,
            "pump_hours": (round(rec.gross_m3 / pump.m3_per_hour, 1)
                           if pump and pump.m3_per_hour else None),
        },
        "state": {
            "last_irrigation": last_irr.isoformat() if last_irr else None,
            "anchored": anchored, "degraded": degraded,
            "ndvi": rec.field.ndvi,
            "ndvi_date": (rec.field.ndvi_date.isoformat()
                          if rec.field.ndvi_date else None),
            "et0_mm": plan.et0_mm if plan else None,
            "kc": plan.kc if plan else None,
            "kc_source": plan.kc_source if plan else None,
            "depletion_mm": plan.depletion_mm if plan else None,
            "raw_mm": plan.raw_mm if plan else None,
            "metered_m3": round(metered_m3),
        },
        "forecast": [
            {"date": (w.date.isoformat() if getattr(w, "date", None) else None),
             "t_max": getattr(w, "t_max", None), "t_min": getattr(w, "t_min", None),
             "rain_mm": getattr(w, "rain_mm", None)}
            for w in (forecast or [])
        ],
        "journal": journal,
        "today": date.today().isoformat(),
    }


@app.get("/api/health")
def health():
    """Для systemd и ручной проверки. Без данных и без авторизации."""
    return JSONResponse({"ok": True, "version": B.__version__})
