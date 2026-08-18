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

    # Экономика сезона: сводка уже умеет считать сама, и её флаги
    # (verified, has_baseline) важнее самих чисел — без них «экономия»
    # читается как измеренная, а она пока расчётная.
    sav = None
    try:
        sv = B.LEDGER.savings(field_id)
        sav = {"recommendations": sv.recommendations, "followed": sv.followed,
               "metered_m3": round(sv.metered_m3), "baseline_m3": round(sv.baseline_m3),
               "saved_m3": round(sv.saved_m3), "verified": sv.verified,
               "has_baseline": sv.has_baseline}
    except Exception as exc:  # noqa: BLE001 — журнал не роняет карточку
        log.warning("экономика по %s не посчиталась: %s", field_id, exc)

    # Отметки полива фермера — что реально происходило на поле.
    with sqlite3.connect(B.LEDGER.path) as c:
        irrigations = [
            {"day": d, "m3": round(m) if m is not None else None,
             "source": src, "recorded_at": ts}
            for d, m, src, ts in c.execute(
                """SELECT a.actual_day, a.actual_m3, a.source, a.recorded_at
                   FROM actions a JOIN recommendations r ON r.id = a.recommendation_id
                   WHERE r.field_id=? AND a.followed=1 AND a.actual_day IS NOT NULL
                   ORDER BY a.actual_day DESC LIMIT 40""", (field_id,))]

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
            "pump_cost_uzs": (round(rec.gross_m3 / pump.m3_per_hour
                                    * pump.uzs_per_hour)
                              if pump and pump.m3_per_hour
                              and getattr(pump, "uzs_per_hour", None) else None),
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
            "etc_mm": plan.etc_mm if plan else None,
            "depletion_mm": plan.depletion_mm if plan else None,
            "raw_mm": plan.raw_mm if plan else None,
            "taw_mm": plan.taw_mm if plan else None,
            "salinity": plan.salinity if plan else None,
        },
        # План водного баланса по дням — то, чего в чат не помещается.
        # Здесь фермер видит не только «когда», но и «почему тогда».
        "plan": [
            {"day": d.day.isoformat(), "etc_mm": round(d.etc_mm, 2),
             "et0_mm": round(d.et0_mm, 2), "kc": round(d.kc, 3),
             "rain_mm": round(d.rain_mm, 1),
             "depletion_mm": round(d.depletion_mm, 1),
             "raw_mm": round(d.raw_mm, 1), "taw_mm": round(d.taw_mm, 1),
             "stress": d.stress, "irrigate": d.irrigate,
             "gross_m3": round(d.gross_m3)}
            for d in (rec.plan or [])],
        "savings": sav,
        "irrigations": irrigations,
        "journal": journal,
        "today": date.today().isoformat(),
    }


@app.get("/api/health")
def health():
    """Для systemd и ручной проверки. Без данных и без авторизации."""
    return JSONResponse({"ok": True, "version": B.__version__})
