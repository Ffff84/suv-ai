#!/usr/bin/env python3
"""
Сколько снимков реально доступно над полем: Sentinel-2 против Landsat.

Скрипт существует ради одного правила проекта: каждая цифра восстановима
(PRODUCT.md). Докстринг suv/landsat.py опирается на замер каталогов —
88 проходов Sentinel-2 в год, 53 годных, худший провал 40 дней, у Landsat
21 дата, которой у Sentinel-2 нет. Все они получены этим скриптом, и
любой может пересчитать их сам или оспорить.

Ничего не меняет и никуда не пишет — только читает каталоги.

    python scripts/landsat_revisit.py --field fields/olma.json
    python scripts/landsat_revisit.py --lat 39.558 --lon 66.996 --days 365

Sentinel-2 берётся из Copernicus (нужны CDSE_CLIENT_ID/SECRET в .env),
Landsat — из Microsoft Planetary Computer анонимно. Если ключей CDSE нет,
считается только Landsat.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

from suv.config import load_env  # noqa: E402

load_env()

PC_STAC = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
CDSE_CATALOG = ("https://sh.dataspace.copernicus.eu/api/v1/"
                "catalog/1.0.0/search")
CDSE_TOKEN = ("https://identity.dataspace.copernicus.eu/auth/realms/CDSE/"
              "protocol/openid-connect/token")


def square(lat: float, lon: float, size_m: float = 200.0) -> list[list[float]]:
    half = size_m / 2.0
    dlat = half / 111_320.0
    dlon = half / (111_320.0 * max(0.2, abs(math.cos(math.radians(lat)))))
    return [[lon - dlon, lat - dlat], [lon + dlon, lat - dlat],
            [lon + dlon, lat + dlat], [lon - dlon, lat + dlat],
            [lon - dlon, lat - dlat]]


def _pages(url: str, body: dict, headers: dict | None = None) -> list[dict]:
    """Все страницы STAC-выдачи. Каталоги отдают по сотне за раз."""
    out: list[dict] = []
    token = None
    while True:
        payload = dict(body)
        if token:
            payload["next"] = token
        r = requests.post(url, json=payload, headers=headers or {}, timeout=60)
        r.raise_for_status()
        d = r.json()
        out.extend(d.get("features", []))
        token = (d.get("context") or {}).get("next")
        if not token:
            return out


def landsat_dates(ring, start: date, end: date) -> list[tuple[date, float]]:
    feats = _pages(PC_STAC, {
        "collections": ["landsat-c2-l2"],
        "intersects": {"type": "Polygon", "coordinates": [ring]},
        "datetime": f"{start.isoformat()}T00:00:00Z/{end.isoformat()}T23:59:59Z",
        "limit": 100,
    })
    return _by_day(feats)


def sentinel_dates(ring, start: date, end: date) -> list[tuple[date, float]]:
    cid = __import__("os").environ.get("CDSE_CLIENT_ID")
    secret = __import__("os").environ.get("CDSE_CLIENT_SECRET")
    if not cid or not secret:
        return []
    t = requests.post(CDSE_TOKEN, data={
        "grant_type": "client_credentials",
        "client_id": cid, "client_secret": secret}, timeout=30)
    t.raise_for_status()
    hdr = {"Authorization": f"Bearer {t.json()['access_token']}"}
    feats = _pages(CDSE_CATALOG, {
        "collections": ["sentinel-2-l2a"],
        "intersects": {"type": "Polygon", "coordinates": [ring]},
        "datetime": f"{start.isoformat()}T00:00:00Z/{end.isoformat()}T23:59:59Z",
        "limit": 100,
    }, hdr)
    return _by_day(feats)


def _by_day(feats) -> list[tuple[date, float]]:
    """Одна запись на день, облачность — минимальная из сцен этого дня.

    Минимум, а не среднее: поле лежит на стыке рядов, и в один день
    приходят две сцены с радикально разной облачностью. Годной делает
    день та, что чище.
    """
    best: dict[date, float] = {}
    for f in feats:
        p = f["properties"]
        d = date.fromisoformat(p["datetime"][:10])
        c = float(p.get("eo:cloud_cover", 100.0))
        best[d] = min(best.get(d, 101.0), c)
    return sorted(best.items())


def report(name: str, days: list[tuple[date, float]], start: date, end: date):
    print(f"\n=== {name} ===")
    if not days:
        print("  нет данных (нет ключей или нет покрытия)")
        return
    print(f"  всего дат: {len(days)}")
    for thr in (10, 20, 40, 60, None):
        good = [d for d, c in days if thr is None or c <= thr]
        if not good:
            continue
        gaps = [(b - a).days for a, b in zip(good, good[1:])]
        gaps += [(good[0] - start).days, (end - good[-1]).days]
        label = "без фильтра" if thr is None else f"облачность <= {thr}%"
        print(f"  {label:20s} дат {len(good):3d}   макс. разрыв "
              f"{max(gaps):3d} дн   медиана "
              f"{sorted(gaps)[len(gaps)//2]:2d} дн")
    return [d for d, c in days if c <= 20]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--field", help="путь к fields/*.json")
    ap.add_argument("--lat", type=float)
    ap.add_argument("--lon", type=float)
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--size", type=float, default=200.0,
                    help="сторона квадрата вокруг точки, м")
    a = ap.parse_args()

    if a.field:
        cfg = json.loads(Path(a.field).read_text(encoding="utf-8"))
        ring = cfg.get("polygon") or square(cfg["lat"], cfg["lon"],
                                            float(cfg.get("field_size_m", a.size)))
        title = f"{cfg.get('name', a.field)} ({cfg.get('hectares', '?')} га)"
    elif a.lat is not None and a.lon is not None:
        ring = square(a.lat, a.lon, a.size)
        title = f"точка {a.lat}, {a.lon}"
    else:
        ap.error("нужен --field или пара --lat/--lon")
        return 2

    end = date.today()
    start = end - timedelta(days=a.days)
    print(f"{title}\nокно: {start} .. {end}  ({a.days} дн.)")

    s2 = sentinel_dates(ring, start, end)
    ls = landsat_dates(ring, start, end)
    s2_good = report("Sentinel-2", s2, start, end) or []
    ls_good = report("Landsat 8/9", ls, start, end) or []

    if s2_good and ls_good:
        extra = [d for d in ls_good if d not in set(s2_good)]
        both = sorted(set(s2_good) | set(ls_good))
        gaps = [(b - a2).days for a2, b in zip(both, both[1:])]
        print(f"\n=== вместе (облачность <= 20%) ===")
        print(f"  Landsat добавляет дат, которых нет у Sentinel-2: "
              f"{len(extra)}")
        print(f"  всего годных дат: {len(both)}   макс. разрыв "
              f"{max(gaps) if gaps else 0} дн")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
