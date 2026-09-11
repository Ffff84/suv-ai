#!/usr/bin/env python3
"""
Пакетный посев полей из файла границ.

    python scripts/import_fields.py granicy.kmz --chat 43348525 \\
        --crop winter_wheat --planting 2026-10-05 --method furrow \\
        --soil loam --prefix GIG --dry-run

Файл: KML / KMZ / GeoJSON / ZIP с shapefile (только WGS84). Один файл —
одна культура и один способ полива: у гиганта клинья однородны, а
исключения правятся точечно после посева. --dry-run печатает разбор и
ничего не пишет.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from suv.config import load_env
load_env()

from datetime import date

from suv.boundary_import import parse_file, seed
from suv.crop import CROPS
from suv.ledger import Ledger
from suv.soil import IRRIGATION_EFFICIENCY, SOILS


def main() -> int:
    ap = argparse.ArgumentParser(description="Границы полей файлом -> база")
    ap.add_argument("file")
    ap.add_argument("--db", default=os.environ.get("SUV_DB", "suv.db"))
    ap.add_argument("--chat", type=int, default=None,
                    help="owner_chat_id владельца (кому слать советы)")
    ap.add_argument("--crop", required=True, choices=sorted(CROPS))
    ap.add_argument("--soil", default="loam", choices=sorted(SOILS))
    ap.add_argument("--method", required=True,
                    choices=sorted(IRRIGATION_EFFICIENCY))
    ap.add_argument("--planting", required=True,
                    help="дата сева/распускания, ГГГГ-ММ-ДД")
    ap.add_argument("--water-table", type=float, default=0.0)
    ap.add_argument("--prefix", default="IMP",
                    help="префикс field_id (IMP -> IMP-001, IMP-002…)")
    ap.add_argument("--elevation", type=float, default=None,
                    help="высота всех полей, м; без неё — запрос по сети")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    date.fromisoformat(args.planting)          # валидация формата

    data = open(args.file, "rb").read()
    fields, problems = parse_file(data, os.path.basename(args.file))
    for p in problems:
        print(f"  ! {p}")
    print(f"Разобрано полей: {len(fields)}, "
          f"всего {sum(f.area_ha for f in fields):.1f} га")
    for f in fields:
        print(f"  {f.name:<30} {f.area_ha:>8.2f} га  "
              f"({f.lat:.5f}, {f.lon:.5f})")
    if args.dry_run or not fields:
        return 0

    led = Ledger(args.db)
    import sqlite3
    with sqlite3.connect(led.path) as c:
        taken = {r[0] for r in c.execute("SELECT field_id FROM fields")}

    counter = [0]

    def make_id() -> str:
        while True:
            counter[0] += 1
            fid = f"{args.prefix}-{counter[0]:03d}"
            if fid not in taken:
                taken.add(fid)
                return fid

    elevation_for = None
    if args.elevation is not None:
        elevation_for = lambda lat, lon: args.elevation  # noqa: E731
    else:
        from suv.weather import fetch_elevation
        elevation_for = fetch_elevation

    created = seed(led, fields, owner_chat=args.chat, crop_key=args.crop,
                   soil_key=args.soil, irrigation_method=args.method,
                   planting_iso=args.planting, make_id=make_id,
                   water_table_m=args.water_table,
                   elevation_for=elevation_for)
    print(f"Посеяно в {led.path}: {len(created)} полей "
          f"({created[0]} … {created[-1]})")
    if args.chat is None:
        print("  ! owner_chat_id не задан: советы слать некому, "
              "поля видны только наблюдателям.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
