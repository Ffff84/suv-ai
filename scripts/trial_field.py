#!/usr/bin/env python3
"""
Опыт «по совету против привычки» на одном поле.

    python scripts/trial_field.py FAR-UZUM --start
    python scripts/trial_field.py FAR-UZUM --start --bearing 45
    python scripts/trial_field.py FAR-UZUM --report --days 60 --csv uzum.csv
    python scripts/trial_field.py FAR-UZUM --stop

--start делит контур на половины вдоль хода воды (из отмеченного входа,
иначе по длинной оси, иначе --bearing) и пишет их в базу + GeoJSON-файл
для карты. Протокол: половина A (левая по ходу воды) поливается по
совету бота, половина B — как привыкли. --report меряет обе половины
Sentinel-2 по каждому годному кадру и печатает разницы; выводы — люди.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from suv.config import load_env
load_env()

from suv.clock import today as today_tashkent
from suv.ledger import Ledger
from suv.trial import flow_bearing_from_inlet, pair_readings, split, summarize


def _row(led, fid):
    import sqlite3
    with sqlite3.connect(led.path) as c:
        c.row_factory = sqlite3.Row
        row = c.execute("SELECT * FROM fields WHERE field_id=?",
                        (fid,)).fetchone()
    if row is None:
        raise SystemExit(f"Поле {fid} не найдено в {led.path}")
    return row


def main() -> int:
    ap = argparse.ArgumentParser(description="Контрольная полоса по-нашему")
    ap.add_argument("field_id")
    ap.add_argument("--db", default=os.environ.get("SUV_DB", "suv.db"))
    ap.add_argument("--start", action="store_true")
    ap.add_argument("--stop", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--bearing", type=float, default=None,
                    help="ход воды, азимут от севера (иначе вход/длинная ось)")
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--from", dest="date_from")
    ap.add_argument("--to", dest="date_to")
    ap.add_argument("--csv")
    ap.add_argument("--out", help="куда писать GeoJSON половин (при --start)")
    args = ap.parse_args()

    led = Ledger(args.db)
    row = _row(led, args.field_id)

    if args.stop:
        led.clear_trial(args.field_id)
        print(f"{args.field_id}: опыт остановлен, половины стёрты "
              "(журнал цел).")
        return 0

    if args.start:
        raw = row["polygon_geojson"]
        if not raw:
            raise SystemExit("У поля нет контура — сначала обведите его "
                             "(бот или import_fields).")
        ring = json.loads(raw)
        bearing = args.bearing
        how = "задан руками"
        if bearing is None and row["inlet_vertices"]:
            i, j = json.loads(row["inlet_vertices"])
            bearing = flow_bearing_from_inlet(ring, i, j)
            how = "из отмеченного входа воды"
        if bearing is None:
            how = "по длинной оси контура (вход не отмечен)"
        ts = split(ring, bearing)
        led.set_trial(args.field_id, json.dumps(ts.half_a),
                      json.dumps(ts.half_b))
        print(f"{row['name']}: опыт начат.")
        print(f"  ход воды {ts.flow_bearing_deg}° — {how}")
        print(f"  A (слева по ходу, ПО СОВЕТУ):  {ts.area_a} га")
        print(f"  B (справа по ходу, ПРИВЫЧКА):  {ts.area_b} га")
        out = args.out or f"trial-{args.field_id}.geojson"
        json.dump({"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {"half": "A — по совету"},
             "geometry": {"type": "Polygon", "coordinates": [ts.half_a]}},
            {"type": "Feature", "properties": {"half": "B — привычка"},
             "geometry": {"type": "Polygon", "coordinates": [ts.half_b]}},
        ]}, open(out, "w", encoding="utf-8"), ensure_ascii=False)
        print(f"  карта половин: {out} (открывается в geojson.io / QGIS)")
        return 0

    if args.report:
        if not row["trial_half_a"]:
            raise SystemExit("Опыт не начат: сначала --start.")
        half_a = json.loads(row["trial_half_a"])
        half_b = json.loads(row["trial_half_b"])
        end = (date.fromisoformat(args.date_to) if args.date_to
               else today_tashkent())
        start = (date.fromisoformat(args.date_from) if args.date_from
                 else end - timedelta(days=args.days))
        from suv.indices import series
        a = series(half_a, start, end)
        b = series(half_b, start, end)
        rows = pair_readings(a, b)
        started = (row["trial_started"] or "")[:10]
        print(f"{row['name']}: опыт с {started or '—'}; "
              f"окно {start}…{end}, парных кадров {len(rows)}")
        print("дата         NDVI A/B (Δ)          NDMI A/B (Δ)      чисто")
        for r in rows:
            print(f"{r.day}  {r.ndvi_a:.3f}/{r.ndvi_b:.3f} ({r.d_ndvi:+.3f})"
                  f"   {r.ndmi_a:+.3f}/{r.ndmi_b:+.3f} ({r.d_ndmi:+.3f})"
                  f"  {r.clear*100:4.0f}%")
        s = summarize(rows)
        if s["days"]:
            print(f"средняя Δ: NDVI {s['mean_d_ndvi']:+.4f}, "
                  f"NDMI {s['mean_d_ndmi']:+.4f} "
                  f"(A минус B; вывод делает человек)")
        if args.csv:
            with open(args.csv, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["date", "ndvi_a", "ndvi_b", "d_ndvi",
                            "ndmi_a", "ndmi_b", "d_ndmi", "clear"])
                for r in rows:
                    w.writerow([r.day, f"{r.ndvi_a:.4f}", f"{r.ndvi_b:.4f}",
                                f"{r.d_ndvi:+.4f}", f"{r.ndmi_a:.4f}",
                                f"{r.ndmi_b:.4f}", f"{r.d_ndmi:+.4f}",
                                f"{r.clear:.2f}"])
            print(f"CSV: {args.csv}")
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
