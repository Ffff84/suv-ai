#!/usr/bin/env python3
"""
Почему бот советует именно эту дату.

    python3 scripts/why_today.py

Показывает по каждому полю пилота всё, из чего сложилась сегодняшняя
рекомендация: дату последнего полива и откуда она взята, снимок со
спутника и его возраст, испарение, коэффициент культуры и накопленный
дефицит против порога.

Зачем. Дата полива законно уточняется каждый день — прогноз и снимки
обновляются, и «в субботу» вчера против «в четверг» сегодня само по себе
не ошибка. Но отличить нормальное уточнение от сломанного расчёта по
одной дате в сообщении нельзя, и вопрос «почему передвинулось» иначе
упирается в догадки. Здесь видно причину: сдвинулся ли Kc, подорожало ли
испарение, не потерялся ли якорь.

С флагом --history печатает журнал прошлых рекомендаций по полю:
когда какая дата советовалась и какой версией движка. Это ground truth,
по которому видно, сдвинул дату новый код или обновившийся прогноз, —
реконструкция «прогнать старый код на сегодняшней погоде» такого
различить не может.

Ничего не меняет — только считает и печатает.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from suv.config import load_env

load_env()

from datetime import date

WD = ("понедельник", "вторник", "среда", "четверг", "пятница",
      "суббота", "воскресенье")
WD2 = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def _history(field_id: str, limit: int) -> None:
    """Что бот советовал по этому полю раньше — прямо из журнала."""
    import sqlite3
    from suv.ledger import Ledger

    with sqlite3.connect(Ledger(os.environ["SUV_DB"]).path) as c:
        rows = c.execute(
            """SELECT generated_on, action_day, et0_mm, kc, depletion_mm,
                      raw_mm, engine_version
               FROM recommendations WHERE field_id=?
               ORDER BY id DESC LIMIT ?""", (field_id, limit)).fetchall()
    if not rows:
        print("     журнал пуст — рекомендаций по полю ещё не записано")
        return
    print(f"     журнал (последние {len(rows)}):")
    print("       посчитано   советовало   ET0    Kc     дефицит/порог  движок")
    for gen, act, et0, kc, dep, raw, ver in rows:
        day = act or "—"
        wd = ""
        if act:
            wd = " (%s)" % WD2[date.fromisoformat(act).weekday()]
        print("       %-11s %s%-6s %-6s %-6s %5.1f/%-6.1f %s"
              % (gen[:10], day, wd, round(et0 or 0, 2), round(kc or 0, 3),
                 dep or 0, raw or 0, ver))


def main() -> int:
    ap = argparse.ArgumentParser(description="Из чего сложилась рекомендация")
    ap.add_argument("--db", default=os.environ.get("SUV_DB", "suv.db"))
    ap.add_argument("--all", action="store_true",
                    help="включая поля, заведённые через /start")
    ap.add_argument("--history", type=int, nargs="?", const=15, default=0,
                    metavar="N", help="журнал последних N рекомендаций")
    args = ap.parse_args()
    os.environ["SUV_DB"] = args.db

    import bot.main as m

    today = date.today()
    print()
    print(f"  Сегодня {today} ({WD[today.weekday()]}), база {args.db}")
    print("  " + "-" * 58)

    rows = [r for r in m._all_fields()
            if args.all or r["field_id"].startswith("FAR-")]
    if not rows:
        print("  Полей нет. Заведите их scripts/seed_field.py.")
        return 1

    for row in rows:
        fid = row["field_id"]
        seeded = row["last_irrigation_date"]
        last = m._last_irrigation(fid, seeded)
        if last is None:
            src = "якоря нет — поле считается только что политым"
        elif seeded and str(last) == seeded:
            src = "из конфига поля"
        else:
            src = "отметка фермера «Suv berdim»"

        rec, _pump, anchored, degraded = m._compute_rec(row, today)
        print(f"  {fid} · {row['name']} · {row['hectares']} га")
        print(f"     последний полив: {last} ({src})")

        f = rec.field
        if f.ndvi is None:
            print("     спутник: снимка нет — Kc по календарю культуры")
        else:
            age = (today - f.ndvi_date).days if f.ndvi_date else None
            print(f"     спутник: NDVI {f.ndvi:.3f}, снимок {f.ndvi_date} "
                  f"({age} дн. назад)")
        if rec.plan:
            p = rec.plan[0]
            print(f"     сегодня: ET0 {p.et0_mm} мм · Kc {p.kc} [{p.kc_source}]"
                  f" · ETc {p.etc_mm} мм")
            print(f"     дефицит {p.depletion_mm} мм из порога {p.raw_mm} мм")
        if rec.action_day is None:
            print(f"     СОВЕТ: полив не требуется ({rec.reason_key})")
        else:
            d = rec.action_day
            print(f"     СОВЕТ: {d} ({WD[d.weekday()]}) · через "
                  f"{rec.days_until} дн. · {rec.gross_mm:.0f} мм = "
                  f"{rec.gross_m3:.0f} м³ · {rec.reason_key}")
        if degraded:
            print("     ! погода не пришла — расчёт по климатическим нормам")
        if not anchored:
            print("     ! якоря нет — дата полива заведомо оптимистична")
        if args.history:
            _history(fid, args.history)
        print()

    print("  " + "-" * 58)
    print("  Дата уточняется каждый день: прогноз и снимки обновляются.")
    print("  Сдвиг на день-два — норма, поэтому дальше недели бот и пишет")
    print("  «ориентировочно». Скачки туда-сюда каждый день — уже нет.")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
