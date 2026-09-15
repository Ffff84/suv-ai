#!/usr/bin/env python3
"""
Триаж уклона: средний градиент поля по открытому рельефу GLO-30.

Бесплатная сортировка полей до дрона и нивелира: у какого поля есть
заметный уклон, куда потечёт вода, и где рельеф вообще неотличим от
плоского при шуме спутникового DEM. Работает для любого поля из
fields/*.json независимо от культуры — рельефу всё равно, что посажено.
Ключей не нужно: GLO-30 читается из открытого бакета AWS (см.
suv/terrain.py — там же, почему не через наш Sentinel Hub).

Ничего не меняет и никуда не пишет — только читает рельеф. Цифры отсюда
НЕ входят в расчёт полива: это разведка для разговора с фермером и для
решения, куда ехать с нивелиром (в докстринге suv/terrain.py — четыре
ограничения метода, от возраста съёмки 2010–2015 до крон вместо почвы).

    python scripts/field_slope.py                          # все fields/*.json
    python scripts/field_slope.py fields/olma.json fields/uzum.json
    python scripts/field_slope.py --lat 39.558 --lon 66.996 --size 200
    python scripts/field_slope.py fields/uzum.json --expand 2

--expand раздувает квадрат вокруг точки: больше пикселей — меньше
стандартная ошибка, но в кадр попадают соседние участки. Допустимо,
пока поля лежат в одном массиве и делят общий склон; полю мельче
~150 м (порог MIN_PIXELS в suv/terrain.py) без этого ключа скрипт
честно откажет.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from suv.satellite import bbox_polygon  # noqa: E402
from suv.terrain import (MIN_PIXELS, fetch_dem, fit_plane,  # noqa: E402
                         gravity_check)


def run_one(title: str, ring: list[list[float]],
            water_inlet: str | None) -> bool:
    print(f"\n=== {title} ===")
    try:
        z, inside, dx, dy = fetch_dem(ring)
    except Exception as exc:  # noqa: BLE001 — одно поле не роняет обход
        print(f"  рельеф не получен ({type(exc).__name__}: {exc})")
        return False

    est = fit_plane(z, inside, dx, dy)
    if est is None:
        print(f"  пикселей рельефа {int(inside.sum())} при пороге "
              f"{MIN_PIXELS} (шаг ~{dx:.0f}x{dy:.0f} м) — на такой сетке "
              "шум проходит за уклон; раздуть квадрат: --expand 2")
        return True

    print(f"  пикселей: {est.n_pixels} (шаг ~{dx:.0f}x{dy:.0f} м)   "
          f"высота: {est.elev_mean:.0f} м, перепад {est.elev_span:.1f} м")
    print(f"  уклон: {est.slope_pct:.2f}% ± {est.se_pct:.2f}%   "
          f"сток: {est.downhill} ({est.aspect_deg:.0f}°)")
    if not est.detectable:
        print("  вердикт: НЕОТЛИЧИМ от нуля при шуме GLO-30 — "
              "для этого поля только нивелир или дрон")
        return True
    print("  вердикт: уклон различим (>= 2·SE)")
    if water_inlet:
        agrees = gravity_check(water_inlet, est.aspect_deg)
        if agrees is True:
            print(f"  вода заходит с направления «{water_inlet}» — "
                  "самотёк согласуется с рельефом")
        elif agrees is False:
            print(f"  вода заходит с направления «{water_inlet}», а сток "
                  f"на {est.downhill} — ПРОТИВОРЕЧИТ рельефу, проверить "
                  "контур и сторону входа")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("fields", nargs="*", help="пути к fields/*.json; "
                    "без аргументов — все поля из fields/")
    ap.add_argument("--lat", type=float)
    ap.add_argument("--lon", type=float)
    ap.add_argument("--size", type=float, default=200.0,
                    help="сторона квадрата вокруг точки --lat/--lon, м")
    ap.add_argument("--expand", type=float, default=1.0,
                    help="во сколько раз раздуть квадрат поля (по умолчанию 1)")
    a = ap.parse_args()

    jobs: list[tuple[str, list[list[float]], str | None]] = []
    if a.lat is not None and a.lon is not None:
        jobs.append((f"точка {a.lat}, {a.lon}",
                     bbox_polygon(a.lat, a.lon, a.size * a.expand), None))
    else:
        paths = ([Path(p) for p in a.fields] if a.fields
                 else sorted((Path(__file__).resolve().parent.parent
                              / "fields").glob("*.json")))
        if not paths:
            ap.error("нет ни fields/*.json, ни пары --lat/--lon")
        for p in paths:
            cfg = json.loads(p.read_text(encoding="utf-8"))
            ring = cfg.get("polygon")
            if ring:
                note = "контур фермера"
            else:
                size = float(cfg.get("field_size_m", a.size)) * a.expand
                ring = bbox_polygon(cfg["lat"], cfg["lon"], size)
                note = f"квадрат {size:.0f} м"
            title = (f"{cfg.get('name', p.stem)} "
                     f"({cfg.get('hectares', '?')} га, "
                     f"{cfg.get('crop', '?')}) — {note}")
            jobs.append((title, ring, cfg.get("water_inlet")))

    ok = sum(run_one(t, r, w) for t, r, w in jobs)

    print("\nПомнить: GLO-30 снят в 2010–2015 и видит кроны, а не почву; "
          "это средний градиент для сортировки полей, а не профиль борозды "
          "для модели Egat.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
