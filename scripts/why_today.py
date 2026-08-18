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


def main() -> int:
    ap = argparse.ArgumentParser(description="Из чего сложилась рекомендация")
    ap.add_argument("--db", default=os.environ.get("SUV_DB", "suv.db"))
    ap.add_argument("--all", action="store_true",
                    help="включая поля, заведённые через /start")
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
        print()

    print("  " + "-" * 58)
    print("  Дата уточняется каждый день: прогноз и снимки обновляются.")
    print("  Сдвиг на день-два — норма, поэтому дальше недели бот и пишет")
    print("  «ориентировочно». Скачки туда-сюда каждый день — уже нет.")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
