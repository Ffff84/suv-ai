"""
Архивная подложка высокого разрешения: тайлы Esri World Imagery.

Та же карта, на которой фермер обводил поле в Mini App, — детализация
меньше метра, видны ряды винограда и отдельные деревья. Sentinel-2 с его
10 м на пиксель такой картинки не даст никогда, поэтому фоном берём её,
а поверх кладём измеренную влажность.

Честность требует двух вещей, и обе — обязанность вызывающего:
 1. подложка АРХИВНАЯ (снята когда-то за последние год-два), и подпись
    обязана это говорить — дата фото относится только к влажности;
 2. Esri требует указания источника — строка атрибуции рисуется прямо
    на кадре и не убирается.

Математика тайлов Web Mercator вынесена в чистые функции: она проверена
тестами без сети, и ошибка «поле уехало на соседний квартал» ловится в
CI, а не на глазах у фермера.
"""

from __future__ import annotations

import math
from io import BytesIO

TILE_URL = ("https://server.arcgisonline.com/ArcGIS/rest/services/"
            "World_Imagery/MapServer/tile/{z}/{y}/{x}")
TILE_PX = 256
ATTRIBUTION = "Esri, Maxar, Earthstar Geographics"

# Пределы зума. 19 — примерно 0,2 м на пиксель на широте Самарканда;
# в сельской местности его может не быть, тогда спускаемся ниже.
MAX_ZOOM = 19
MIN_ZOOM = 15


def mercator_px(lon: float, lat: float, zoom: int) -> tuple[float, float]:
    """Глобальные пиксельные координаты Web Mercator на данном зуме."""
    scale = TILE_PX * (2 ** zoom)
    x = (lon + 180.0) / 360.0 * scale
    rad = math.radians(lat)
    y = (1.0 - math.log(math.tan(rad) + 1.0 / math.cos(rad)) / math.pi) / 2.0 * scale
    return x, y


def pick_zoom(box: tuple[float, float, float, float],
              want_px: int = 1000) -> int:
    """Зум, при котором ширина кадра близка к want_px, в пределах допуска.

    Чем выше зум, тем чётче — но тем больше тайлов качать и тем выше
    шанс, что покрытия на этом уровне нет.
    """
    lon0, lat0, lon1, lat1 = box
    for z in range(MAX_ZOOM, MIN_ZOOM - 1, -1):
        x0, _ = mercator_px(lon0, lat0, z)
        x1, _ = mercator_px(lon1, lat1, z)
        if abs(x1 - x0) <= want_px:
            return z
    return MIN_ZOOM


def tile_range(box: tuple[float, float, float, float],
               zoom: int) -> tuple[int, int, int, int]:
    """(tx0, ty0, tx1, ty1) — номера тайлов, покрывающие bbox."""
    lon0, lat0, lon1, lat1 = box
    x0, y1 = mercator_px(lon0, lat0, zoom)   # юг — низ, у меркатора y растёт вниз
    x1, y0 = mercator_px(lon1, lat1, zoom)
    return (int(x0 // TILE_PX), int(y0 // TILE_PX),
            int(x1 // TILE_PX), int(y1 // TILE_PX))


def crop_window(box: tuple[float, float, float, float], zoom: int,
                tx0: int, ty0: int) -> tuple[int, int, int, int]:
    """Окно bbox в пикселях склеенного полотна тайлов."""
    lon0, lat0, lon1, lat1 = box
    x0, y1 = mercator_px(lon0, lat0, zoom)
    x1, y0 = mercator_px(lon1, lat1, zoom)
    ox, oy = tx0 * TILE_PX, ty0 * TILE_PX
    return (int(round(x0 - ox)), int(round(y0 - oy)),
            int(round(x1 - ox)), int(round(y1 - oy)))


def looks_blank(img) -> bool:
    """Заглушка «Map data not yet available» вместо снимка.

    Esri отдаёт её с кодом 200, поэтому по HTTP дырку в покрытии не
    поймать. Ловим по содержимому: заглушка — почти сплошной серый,
    у настоящего снимка поля такой доли одного цвета не бывает.
    """
    thumb = img.convert("RGB").resize((64, 64))
    colors = thumb.getcolors(64 * 64)
    if not colors:
        return False
    dominant = max(n for n, _c in colors)
    return dominant / (64 * 64) > 0.5


def fetch(box: tuple[float, float, float, float],
          timeout: int = 20):
    """Скачать и склеить подложку под bbox. PIL.Image или исключение.

    Ошибка сети или дырка в покрытии — не повод остаться без фото:
    вызывающий ловит исключение и падает обратно на кадр Sentinel-2.
    """
    import requests
    from PIL import Image

    zoom = pick_zoom(box)
    last_exc: Exception | None = None
    for z in range(zoom, MIN_ZOOM - 1, -1):
        tx0, ty0, tx1, ty1 = tile_range(box, z)
        n_tiles = (tx1 - tx0 + 1) * (ty1 - ty0 + 1)
        if n_tiles > 30:
            continue  # bbox слишком велик для этого зума — ниже
        sheet = Image.new("RGB", ((tx1 - tx0 + 1) * TILE_PX,
                                  (ty1 - ty0 + 1) * TILE_PX))
        try:
            for ty in range(ty0, ty1 + 1):
                for tx in range(tx0, tx1 + 1):
                    r = requests.get(TILE_URL.format(z=z, y=ty, x=tx),
                                     timeout=timeout)
                    r.raise_for_status()
                    tile = Image.open(BytesIO(r.content)).convert("RGB")
                    sheet.paste(tile, ((tx - tx0) * TILE_PX,
                                       (ty - ty0) * TILE_PX))
        except Exception as exc:  # noqa: BLE001 — пробуем зум ниже
            last_exc = exc
            continue
        cut = sheet.crop(crop_window(box, z, tx0, ty0))
        if looks_blank(cut):
            continue  # на этом зуме покрытия нет — ниже оно обычно есть
        return cut
    raise last_exc or RuntimeError("подложки нет ни на одном зуме")
