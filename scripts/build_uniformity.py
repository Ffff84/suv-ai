#!/usr/bin/env python3
"""
Собрать замер равномерности полива по многолетнему композиту.

    python scripts/build_uniformity.py FAR-UZUM
    python scripts/build_uniformity.py --all

Сетевая работа: до 3 кадров за сезон × до 7 сезонов на поле. Результат
пишется в fields.uniformity_json; экран «Dala holati» его только
читает. Пересборка — раз в месяц-два или после смены контура.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from suv.config import load_env
load_env()

from suv.ledger import Ledger
from suv.trial import default_flow_bearing, flow_bearing_from_inlet
from suv.uniformity import build


def main() -> int:
    ap = argparse.ArgumentParser(description="Равномерность по композиту")
    ap.add_argument("field_id", nargs="?")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--db", default=os.environ.get("SUV_DB", "suv.db"))
    ap.add_argument("--years", type=int, default=6)
    args = ap.parse_args()
    if not args.field_id and not args.all:
        ap.error("нужен FIELD_ID или --all")

    led = Ledger(args.db)
    import sqlite3
    with sqlite3.connect(led.path) as c:
        c.row_factory = sqlite3.Row
        if args.all:
            rows = c.execute(
                "SELECT * FROM fields WHERE polygon_geojson IS NOT NULL "
                "AND archived_at IS NULL").fetchall()
        else:
            rows = c.execute("SELECT * FROM fields WHERE field_id=?",
                             (args.field_id,)).fetchall()
    if not rows:
        raise SystemExit("Нет полей с контуром — замер строится по контуру.")

    for row in rows:
        ring = json.loads(row["polygon_geojson"])
        if row["inlet_vertices"]:
            i, j = json.loads(row["inlet_vertices"])
            bearing, source = flow_bearing_from_inlet(ring, i, j), "inlet"
        else:
            bearing, source = default_flow_bearing(ring), "long_axis"
        print(f"{row['field_id']} ({row['name']}): ход воды {bearing}° "
              f"[{source}], собираю до {args.years} сезонов…")
        payload = build(ring, bearing, source, years=args.years)
        led.save_uniformity(row["field_id"], json.dumps(payload))
        if payload.get("refused"):
            print(f"  честный отказ: {payload['refused']} "
                  f"(сезонов {payload['seasons_used']}, "
                  f"стабильно {payload['stable_share']:.0%})")
        else:
            print(f"  сезонов {payload['seasons_used']} · стабильно "
                  f"{payload['stable_share']:.0%} · дальний край "
                  f"{payload['tail_pct']:+.1f}% (кадров {payload['scenes_used']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
