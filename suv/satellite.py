"""
Sentinel-2 NDVI per field.

Copernicus Data Space Ecosystem is free and open; registration takes
minutes and gives OAuth credentials for the Sentinel Hub Process API.
Revisit over Uzbekistan is 3-5 days with two satellites.

This module is written against the real API but is NOT exercised in CI,
because the test environment has no outbound network. Treat it as
reviewed-but-unproven until it has run against a live token — that
distinction is deliberate and should survive into the pitch.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date, timedelta

import requests

TOKEN_URL = ("https://identity.dataspace.copernicus.eu/auth/realms/CDSE/"
             "protocol/openid-connect/token")
PROCESS_URL = "https://sh.dataspace.copernicus.eu/api/v1/process"

# Mean NDVI over the field polygon, cloud-masked via the scene
# classification band. Returns a single number per request.
#
# Три канала, а не два: «чистый пиксель» и «пиксель внутри полигона» —
# разные вещи. Раньше оба сливались в один бэнд, и доля облачности
# считалась от ВСЕГО прямоугольника кадра: вытянутое поле, занимающее
# треть своего bbox, выбраковывалось как «в облаках» при чистом небе —
# ровно та ошибка, которую field_photo.stats_over_field запрещает со
# ссылкой на ТЗ §4.2. Третий бэнд отделяет геометрию от погоды.
EVALSCRIPT = """
//VERSION=3
function setup() {
  return {
    input: [{bands: ["B04", "B08", "SCL", "dataMask"]}],
    output: {bands: 3, sampleType: "FLOAT32"}
  };
}
function evaluatePixel(s) {
  if (s.dataMask == 0) return [0, 0, 0];
  // SCL 3=cloud shadow, 8/9=cloud medium/high, 10=cirrus, 11=snow
  var bad = (s.SCL == 3 || s.SCL == 8 || s.SCL == 9 || s.SCL == 10 || s.SCL == 11);
  if (bad) return [0, 0, 1];
  var ndvi = (s.B08 - s.B04) / (s.B08 + s.B04);
  return [ndvi, 1, 1];
}
"""

CATALOG_URL = ("https://sh.dataspace.copernicus.eu/api/v1/"
               "catalog/1.0.0/search")


@dataclass
class NdviReading:
    value: float
    observed_on: date
    valid_fraction: float  # share of the field that was cloud-free


def get_token(client_id: str | None = None,
              client_secret: str | None = None) -> str:
    client_id = client_id or os.environ["CDSE_CLIENT_ID"]
    client_secret = client_secret or os.environ["CDSE_CLIENT_SECRET"]
    r = requests.post(TOKEN_URL, data={
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


def fetch_ndvi(polygon: list[list[float]], token: str,
               window_days: int = 12,
               today: date | None = None) -> NdviReading | None:
    """
    Mean cloud-free NDVI over a field polygon (GeoJSON ring, lon/lat).

    Returns None when every scene in the window is clouded out — the
    caller must then fall back to the crop calendar rather than reuse a
    stale value, which blended_kc() handles explicitly.
    """
    today = today or date.today()
    start = today - timedelta(days=window_days)

    body = {
        "input": {
            "bounds": {"geometry": {"type": "Polygon", "coordinates": [polygon]}},
            "data": [{
                "type": "sentinel-2-l2a",
                "dataFilter": {
                    "timeRange": {
                        "from": f"{start.isoformat()}T00:00:00Z",
                        "to": f"{today.isoformat()}T23:59:59Z",
                    },
                    "maxCloudCoverage": 60,
                    "mosaickingOrder": "mostRecent",
                },
            }],
        },
        "aggregation": None,
        "output": {"width": 64, "height": 64,
                   "responses": [{"identifier": "default", "format": {"type": "image/tiff"}}]},
        "evalscript": EVALSCRIPT,
    }

    r = requests.post(PROCESS_URL, json=body, timeout=60,
                      headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    observed = _latest_scene_date(polygon, token, start, today)
    if observed is None:
        # Каталог не ответил — датируем НАЧАЛОМ окна, а не сегодняшним
        # днём. «or today» здесь возвращал ровно ту ошибку, от которой
        # _latest_scene_date защищает: 12-дневный NDVI получал вес
        # свежего (70% вместо 10%) всякий раз, когда каталог таймаутил,
        # а process-запрос проходил. Консервативная дата занижает вес —
        # это честная цена за неизвестность.
        observed = start
    return _reduce_tiff(r.content, observed)


def _latest_scene_date(polygon: list[list[float]], token: str,
                       start: date, end: date,
                       max_cloud: int = 60) -> date | None:
    """
    Дата последней сцены Sentinel-2 над полем в окне запроса.

    Раньше снимок датировался днём ЗАПРОСА (observed_on=today), из-за
    чего blended_kc давал десятидневному NDVI вес свежего — ровно та
    ошибка, от которой распад веса по возрасту и должен защищать.
    mostRecent-мозаика кладёт сверху самую свежую сцену, прошедшую фильтр
    облачности, — её дату и берём. Любой сбой каталога -> None: расчёт
    не падает, просто датируем днём запроса, как раньше.
    """
    body = {
        "collections": ["sentinel-2-l2a"],
        "datetime": f"{start.isoformat()}T00:00:00Z/{end.isoformat()}T23:59:59Z",
        "intersects": {"type": "Polygon", "coordinates": [polygon]},
        "limit": 50,
        "filter": {"op": "<=", "args": [{"property": "eo:cloud_cover"},
                                        max_cloud]},
        "filter-lang": "cql2-json",
    }
    try:
        r = requests.post(CATALOG_URL, json=body, timeout=30,
                          headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()
        dates = [f["properties"]["datetime"][:10]
                 for f in r.json().get("features", [])]
        return date.fromisoformat(max(dates)) if dates else None
    except Exception as exc:  # noqa: BLE001 — деградация, не отказ
        logging.getLogger("suv.satellite").warning(
            "каталог сцен не ответил (%s: %s) — датирую снимок началом окна",
            type(exc).__name__, exc)
        return None


def _reduce_tiff(content: bytes, observed_on: date) -> NdviReading | None:
    """Average the cloud-free pixels of the returned 3-band GeoTIFF.

    Доля облачности — от пикселей ВНУТРИ полигона (бэнд 3), не от всего
    растра: иначе она мерила бы, какую часть прямоугольника занимает
    поле, и вытянутый участок отвергался бы в ясный день.
    """
    try:
        import numpy as np  # noqa: F401 — rasterio без него не читает
        import rasterio
        from io import BytesIO
        with rasterio.open(BytesIO(content)) as src:
            ndvi = src.read(1).astype("float32")
            clear = src.read(2).astype("float32") > 0
            inside = src.read(3).astype("float32") > 0
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError("fetch_ndvi needs rasterio + numpy installed") from exc

    in_field = int(inside.sum())
    if not in_field:
        return None
    frac = float(clear.sum()) / in_field
    if frac < 0.30:
        return None  # too clouded to trust
    return NdviReading(value=float(ndvi[clear].mean()),
                       observed_on=observed_on, valid_fraction=frac)


def bbox_polygon(lat: float, lon: float, size_m: float = 200.0) -> list[list[float]]:
    """Square polygon around a point — good enough to demo before a
    farmer has drawn his real field boundary.

    size_m — СТОРОНА квадрата, как обещают README и field.example.json.
    Раньше это была полусторона: «квадрат 200 м» выходил 400x400 м =
    16 га, и NDVI двухгектарного сада на 7/8 состоял из соседних
    участков и дороги.
    """
    half = size_m / 2.0
    dlat = half / 111_320.0
    dlon = half / (111_320.0 * max(0.2, abs(__import__("math").cos(__import__("math").radians(lat)))))
    return [
        [lon - dlon, lat - dlat], [lon + dlon, lat - dlat],
        [lon + dlon, lat + dlat], [lon - dlon, lat + dlat],
        [lon - dlon, lat - dlat],
    ]
