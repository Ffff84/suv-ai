"""
Кадры Sentinel-2 для фото поля: подложка и влажность.

Отдельно от satellite.py, где живёт NDVI для расчёта: там нужен один
средний по полю индекс, здесь — растр по пикселям. Ключ и точка входа
Copernicus те же самые.

Как и satellite.py, в CI не гоняется: у тестового окружения нет сети.
Проверяется живым ключом на реальном поле.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from io import BytesIO

import requests

from .satellite import CATALOG_URL, PROCESS_URL, get_token

# Усиление яркости для агроландшафта (ТЗ §4.2): без него поля выходят
# почти чёрными и фермер не узнаёт своё.
TRUE_COLOR_GAIN = 2.8

# Облака, тени и снег по классификации сцены. Пиксель с такой меткой
# ничего не говорит о поле, и притворяться, что говорит, нельзя.
BAD_SCL = (0, 1, 3, 8, 9, 10, 11)

TRUE_COLOR_EVAL = f"""
//VERSION=3
function setup() {{
  return {{input: [{{bands: ["B04","B03","B02","SCL","dataMask"]}}],
          output: {{bands: 4, sampleType: "UINT8"}}}};
}}
function evaluatePixel(s) {{
  var bad = {list(BAD_SCL)};
  var a = (bad.indexOf(s.SCL) === -1 && s.dataMask) ? 255 : 0;
  var g = {TRUE_COLOR_GAIN};
  return [Math.min(255, s.B04*255*g), Math.min(255, s.B03*255*g),
          Math.min(255, s.B02*255*g), a];
}}
"""

# NDMI = (B08 - B11) / (B08 + B11). B11 — коротковолновый инфракрасный,
# он и чувствует воду в листе и в верхнем слое почвы.
MOISTURE_EVAL = f"""
//VERSION=3
function setup() {{
  return {{input: [{{bands: ["B08","B11","SCL","dataMask"]}}],
          output: {{bands: 2, sampleType: "FLOAT32"}}}};
}}
function evaluatePixel(s) {{
  var bad = {list(BAD_SCL)};
  if (bad.indexOf(s.SCL) !== -1 || s.dataMask == 0) return [0, 0];
  return [(s.B08 - s.B11) / (s.B08 + s.B11), 1];
}}
"""

WGS84 = "http://www.opengis.net/def/crs/EPSG/0/4326"


@dataclass
class Scene:
    """Один кадр над полем."""

    day: date | None
    rgb: object                 # PIL.Image, подложка
    ndmi: list[list[float]]     # влажность по пикселям
    valid: list[list[bool]]     # чистые от облаков пиксели


def _process(token: str, box, width: int, height: int, evalscript: str,
             fmt: str, days: int, timeout: int = 45) -> bytes:
    """Один запрос к Process API. Таймаут 45 с по ТЗ §7."""
    today = date.today()
    body = {
        "input": {
            "bounds": {"bbox": list(box), "properties": {"crs": WGS84}},
            "data": [{
                "type": "sentinel-2-l2a",
                "dataFilter": {
                    "timeRange": {
                        "from": f"{(today - timedelta(days=days)).isoformat()}T00:00:00Z",
                        "to": f"{today.isoformat()}T23:59:59Z"},
                    # Самый чистый кадр окна, а не самый свежий: облако
                    # над полем бесполезнее, чем снимок трёхдневной давности.
                    "mosaickingOrder": "leastCC"},
            }],
        },
        "output": {"width": width, "height": height,
                   "responses": [{"identifier": "default",
                                  "format": {"type": fmt}}]},
        "evalscript": evalscript,
    }
    r = requests.post(PROCESS_URL, json=body, timeout=timeout,
                      headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    return r.content


def latest_scene_day(token: str, box, days: int = 20,
                     max_cloud: int = 60) -> date | None:
    """Дата самого свежего кадра над полем.

    Без неё подпись «снимок от такого-то числа» пришлось бы ставить днём
    запроса — и фермер решил бы, что смотрит на сегодня (ТЗ §1.2).
    """
    lon0, lat0, lon1, lat1 = box
    today = date.today()
    body = {
        "collections": ["sentinel-2-l2a"],
        "datetime": (f"{(today - timedelta(days=days)).isoformat()}T00:00:00Z/"
                     f"{today.isoformat()}T23:59:59Z"),
        "bbox": [lon0, lat0, lon1, lat1],
        "limit": 50,
        "filter": {"op": "<=", "args": [{"property": "eo:cloud_cover"},
                                        max_cloud]},
        "filter-lang": "cql2-json",
    }
    try:
        r = requests.post(CATALOG_URL, json=body, timeout=30,
                          headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()
        days_found = [f["properties"]["datetime"][:10]
                      for f in r.json().get("features", [])]
        return date.fromisoformat(max(days_found)) if days_found else None
    except Exception:  # noqa: BLE001 — без даты фото не строим, но и не падаем
        return None


def native_size(box, metres_per_pixel: float = 10.0) -> tuple[int, int]:
    """Размер растра в НАТИВНОМ разрешении спутника.

    Просить у сервера сразу 1080 пикселей нельзя: он отдаст сглаженную
    картинку, и блочность 10 м — единственное, что показывает фермеру
    настоящую точность данных, — исчезнет.
    """
    lon0, lat0, lon1, lat1 = box
    mid = math.radians((lat0 + lat1) / 2.0)
    w_m = (lon1 - lon0) * 111_320.0 * math.cos(mid)
    h_m = (lat1 - lat0) * 110_540.0
    return (max(4, round(w_m / metres_per_pixel)),
            max(4, round(h_m / metres_per_pixel)))


def fetch(box, token: str | None = None, days: int = 20) -> Scene:
    """Подложка и влажность над полем одним заходом.

    Влажность просится в той же сетке, что и подложка, хотя её нативные
    20 м вдвое грубее: сервер сам разложит 20-метровые значения по
    10-метровым клеткам, и совмещать два растра вручную не придётся.
    """
    from PIL import Image

    token = token or get_token()
    w, h = native_size(box)
    rgb = Image.open(BytesIO(_process(token, box, w, h, TRUE_COLOR_EVAL,
                                      "image/png", days))).convert("RGB")

    import numpy as np
    import rasterio
    raw = _process(token, box, w, h, MOISTURE_EVAL, "image/tiff", days)
    with rasterio.open(BytesIO(raw)) as src:
        ndmi = np.asarray(src.read(1), dtype="float64")
        mask = np.asarray(src.read(2), dtype="float64")

    return Scene(day=latest_scene_day(token, box, days), rgb=rgb,
                 ndmi=ndmi.tolist(), valid=(mask > 0).tolist())
