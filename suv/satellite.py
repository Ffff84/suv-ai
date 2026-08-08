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

import os
from dataclasses import dataclass
from datetime import date, timedelta

import requests

TOKEN_URL = ("https://identity.dataspace.copernicus.eu/auth/realms/CDSE/"
             "protocol/openid-connect/token")
PROCESS_URL = "https://sh.dataspace.copernicus.eu/api/v1/process"

# Mean NDVI over the field polygon, cloud-masked via the scene
# classification band. Returns a single number per request.
EVALSCRIPT = """
//VERSION=3
function setup() {
  return {
    input: [{bands: ["B04", "B08", "SCL", "dataMask"]}],
    output: {bands: 2, sampleType: "FLOAT32"}
  };
}
function evaluatePixel(s) {
  // SCL 3=cloud shadow, 8/9=cloud medium/high, 10=cirrus, 11=snow
  var bad = (s.SCL == 3 || s.SCL == 8 || s.SCL == 9 || s.SCL == 10 || s.SCL == 11);
  if (bad || s.dataMask == 0) return [0, 0];
  var ndvi = (s.B08 - s.B04) / (s.B08 + s.B04);
  return [ndvi, 1];
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
    observed = _latest_scene_date(polygon, token, start, today) or today
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
    except Exception:  # noqa: BLE001 — деградация, не отказ
        return None


def _reduce_tiff(content: bytes, observed_on: date) -> NdviReading | None:
    """Average the valid pixels of the returned 2-band GeoTIFF."""
    try:
        import numpy as np
        import rasterio
        from io import BytesIO
        with rasterio.open(BytesIO(content)) as src:
            ndvi = src.read(1).astype("float32")
            mask = src.read(2).astype("float32")
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError("fetch_ndvi needs rasterio + numpy installed") from exc

    valid = mask > 0
    frac = float(valid.mean())
    if frac < 0.30:
        return None  # too clouded to trust
    return NdviReading(value=float(ndvi[valid].mean()),
                       observed_on=observed_on, valid_fraction=frac)


def bbox_polygon(lat: float, lon: float, size_m: float = 200.0) -> list[list[float]]:
    """Square polygon around a point — good enough to demo before a
    farmer has drawn his real field boundary."""
    dlat = size_m / 111_320.0
    dlon = size_m / (111_320.0 * max(0.2, abs(__import__("math").cos(__import__("math").radians(lat)))))
    return [
        [lon - dlon, lat - dlat], [lon + dlon, lat - dlat],
        [lon + dlon, lat + dlat], [lon - dlon, lat + dlat],
        [lon - dlon, lat - dlat],
    ]
