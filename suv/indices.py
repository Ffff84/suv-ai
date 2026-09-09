"""
Ряды спутниковых индексов по полю — инструмент ретро-разбора.

Разбор сезона-2025 по саду Фарруха держался на двух рядах: NDMI (вода в
листе) и NDVI (полог). Этот модуль снимает те же ряды одним заходом и
добавляет два индекса, которых не хватало:

* NDRE (red-edge, канал B05) — хлорофилл. На плотном пологе NDVI
  «насыщается» и перестаёт различать стресс; NDRE различает дольше,
  поэтому подпись стресса NDMI -> NDRE -> NDVI видна раньше и на
  густом хлопке. Внимание: B05 у Sentinel-2 идёт в 20 м — по среднему
  на поле это не мешает, по карте был бы грубее.
* MSAVI (Qi et al. 1994) — полог без вклада яркости почвы: тёмная
  мокрая земля после полива не рисует «прирост кроны».

Правила те же, что во всём спутниковом слое: дата сцены прибита к
запросу намертво (никаких мозаик «самое чистое за окно»), доля чистых
пикселей считается от поля, а не от прямоугольника кадра, слепой кадр
(<30% чистых) не притворяется замером.

Сетевой слой НЕ гоняется в CI (окружение без сети) — как satellite.py:
reviewed-but-unproven до живого запуска; чистая математика под тестами.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import requests

from .satellite import (CATALOG_URL, PROCESS_URL, get_token,
                        reduce_index_arrays)

# Шесть выходов: четыре индекса + «чистый» + «внутри полигона».
# BAD_SCL как в scene.py: облака, тени, циррус, снег.
INDEX_EVAL = """
//VERSION=3
function setup() {
  return {
    input: [{bands: ["B04", "B05", "B08", "B11", "SCL", "dataMask"]}],
    output: {bands: 6, sampleType: "FLOAT32"}
  };
}
function evaluatePixel(s) {
  if (s.dataMask == 0) return [0, 0, 0, 0, 0, 0];
  var bad = (s.SCL == 3 || s.SCL == 8 || s.SCL == 9 || s.SCL == 10 || s.SCL == 11);
  if (bad) return [0, 0, 0, 0, 0, 1];
  var ndvi = (s.B08 - s.B04) / (s.B08 + s.B04);
  var t = 2.0 * s.B08 + 1.0;
  var msavi = (t - Math.sqrt(t * t - 8.0 * (s.B08 - s.B04))) / 2.0;
  var ndre = (s.B08 - s.B05) / (s.B08 + s.B05);
  var ndmi = (s.B08 - s.B11) / (s.B08 + s.B11);
  return [ndvi, msavi, ndre, ndmi, 1, 1];
}
"""


@dataclass
class IndexReading:
    """Средние индексы одного кадра по полю."""

    day: date
    ndvi: float
    msavi: float
    ndre: float
    ndmi: float
    valid_fraction: float


def scene_days(polygon: list[list[float]], token: str,
               start: date, end: date) -> list[date]:
    """Все дни, когда Sentinel-2 проходил над полем в окне, от старых к
    новым. Облачность сцены НЕ фильтруем: годность решается по доле
    чистых пикселей внутри контура, а это видно только по самому кадру."""
    days: set[date] = set()
    body = {
        "collections": ["sentinel-2-l2a"],
        "datetime": f"{start.isoformat()}T00:00:00Z/{end.isoformat()}T23:59:59Z",
        "intersects": {"type": "Polygon", "coordinates": [polygon]},
        "limit": 100,
    }
    r = requests.post(CATALOG_URL, json=body, timeout=30,
                      headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    for f in r.json().get("features", []):
        days.add(date.fromisoformat(f["properties"]["datetime"][:10]))
    return sorted(days)


def fetch_day(polygon: list[list[float]], day: date,
              token: str) -> IndexReading | None:
    """Индексы одного конкретного дня. None — кадр слепой (облака)."""
    body = {
        "input": {
            "bounds": {"geometry": {"type": "Polygon", "coordinates": [polygon]}},
            "data": [{
                "type": "sentinel-2-l2a",
                "dataFilter": {
                    "timeRange": {"from": f"{day.isoformat()}T00:00:00Z",
                                  "to": f"{day.isoformat()}T23:59:59Z"}},
            }],
        },
        "output": {"width": 64, "height": 64,
                   "responses": [{"identifier": "default",
                                  "format": {"type": "image/tiff"}}]},
        "evalscript": INDEX_EVAL,
    }
    r = requests.post(PROCESS_URL, json=body, timeout=60,
                      headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()

    try:
        import rasterio
        from io import BytesIO
        with rasterio.open(BytesIO(r.content)) as src:
            bands = [src.read(i).astype("float32") for i in range(1, 7)]
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError("indices need rasterio + numpy installed") from exc

    ndvi, msavi, ndre, ndmi = bands[0], bands[1], bands[2], bands[3]
    clear, inside = bands[4] > 0, bands[5] > 0
    got = reduce_index_arrays(clear, inside, ndvi, msavi, ndre, ndmi)
    if got is None:
        return None
    n, m, re_, w, frac = got
    return IndexReading(day=day, ndvi=n, msavi=m, ndre=re_, ndmi=w,
                        valid_fraction=frac)


def series(polygon: list[list[float]], start: date, end: date,
           token: str | None = None) -> list[IndexReading]:
    """Ряд индексов по всем годным кадрам окна, от старых к новым.

    Один process-запрос на кадр: за сезон это ~40-70 запросов — в
    пределах квоты CDSE, но окно в несколько лет снимайте сознательно.
    """
    token = token or get_token()
    out: list[IndexReading] = []
    for day in scene_days(polygon, token, start, end):
        reading = fetch_day(polygon, day, token)
        if reading is not None:
            out.append(reading)
    return out
