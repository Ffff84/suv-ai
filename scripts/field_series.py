#!/usr/bin/env python3
"""
Ряды спутниковых индексов по полю — сырьё для ретро-разбора.

    python scripts/field_series.py fields/olma.json --days 120
    python scripts/field_series.py fields/olma.json --from 2025-06-01 --to 2025-09-30 --csv olma-2025.csv

Печатает таблицу NDVI / MSAVI / NDRE / NDMI по каждому годному кадру и,
если задан --csv, пишет тот же ряд в файл. Это инструмент разбора
(«докажи стресс задним числом»), а не совет фермеру: выводы из ряда
делает человек, по методике разбора-2025 (NDMI падает первым, NDRE
вторым, NDVI последним).
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from suv.config import load_env
load_env()

from suv.clock import today as today_tashkent
from suv.satellite import bbox_polygon


def main() -> int:
    ap = argparse.ArgumentParser(description="Ряд индексов Sentinel-2 по полю")
    ap.add_argument("field", help="fields/*.json с координатами поля")
    ap.add_argument("--days", type=int, default=120,
                    help="окно назад от сегодня, дней (умолчание 120)")
    ap.add_argument("--from", dest="date_from", help="начало окна, ГГГГ-ММ-ДД")
    ap.add_argument("--to", dest="date_to", help="конец окна, ГГГГ-ММ-ДД")
    ap.add_argument("--csv", help="куда писать CSV (иначе только таблица)")
    args = ap.parse_args()

    if not os.environ.get("CDSE_CLIENT_ID"):
        raise SystemExit("Нужны CDSE_CLIENT_ID/CDSE_CLIENT_SECRET в .env — "
                         "ряд снимается из Copernicus.")

    import json
    cfg = json.load(open(args.field, encoding="utf-8"))
    poly = cfg.get("polygon") or bbox_polygon(
        float(cfg["lat"]), float(cfg["lon"]),
        float(cfg.get("field_size_m", 200)))

    end = date.fromisoformat(args.date_to) if args.date_to else today_tashkent()
    start = (date.fromisoformat(args.date_from) if args.date_from
             else end - timedelta(days=args.days))

    from suv.indices import series
    rows = series(poly, start, end)
    name = cfg.get("name", cfg.get("field_id", args.field))
    print(f"{name}: {start} … {end}, годных кадров {len(rows)}")
    print("дата         NDVI  MSAVI   NDRE   NDMI  чистых")
    for r in rows:
        print(f"{r.day}  {r.ndvi:5.3f}  {r.msavi:5.3f}  {r.ndre:5.3f} "
              f" {r.ndmi:6.3f}  {r.valid_fraction*100:4.0f}%")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["date", "ndvi", "msavi", "ndre", "ndmi",
                        "valid_fraction"])
            for r in rows:
                w.writerow([r.day.isoformat(), f"{r.ndvi:.4f}",
                            f"{r.msavi:.4f}", f"{r.ndre:.4f}",
                            f"{r.ndmi:.4f}", f"{r.valid_fraction:.3f}"])
        print(f"CSV: {args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
