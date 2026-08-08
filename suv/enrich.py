"""
Подключение спутника к конкретному полю.

Отдельный модуль, потому что и расчёт, и бот должны обогащать поле
одинаково. Раньше fetch_ndvi вызывался только в диагностике: спутник
формально работал, но на рекомендацию не влиял вообще — самый неприятный
вид дыры, потому что снаружи всё выглядит рабочим.

Правило: обогащение НИКОГДА не роняет расчёт. Нет ключей, нет сети,
сплошная облачность — движок продолжает считать по календарю культуры и
честно сообщает, что спутника не было.
"""

from __future__ import annotations

import os
from datetime import date

from .satellite import bbox_polygon, fetch_ndvi, get_token


def attach_ndvi(fld, field_size_m: float = 200.0,
                window_days: int = 12, polygon: list | None = None) -> str:
    """
    Дописать в поле свежий NDVI. Возвращает строку статуса для лога.

    polygon — реальные границы участка (GeoJSON ring, [[lon,lat],...]).
    Если их нет, берём квадрат вокруг точки: для пилота этого достаточно,
    но квадрат может захватить соседнюю культуру или дорогу, поэтому
    настоящие границы всегда лучше.
    """
    if not os.environ.get("CDSE_CLIENT_ID") or not os.environ.get("CDSE_CLIENT_SECRET"):
        return "спутник не подключён — считаю по календарю культуры"

    try:
        token = get_token()
    except Exception as exc:  # noqa: BLE001
        return f"спутник: ошибка авторизации ({type(exc).__name__})"

    poly = polygon or bbox_polygon(fld.lat, fld.lon, field_size_m)
    try:
        reading = fetch_ndvi(poly, token, window_days=window_days)
    except Exception as exc:  # noqa: BLE001
        return f"спутник: снимок не получен ({type(exc).__name__})"

    if reading is None:
        return (f"спутник: за {window_days} дн. все снимки в облаках — "
                f"считаю по календарю")

    fld.ndvi = reading.value
    fld.ndvi_date = reading.observed_on
    return (f"спутник: NDVI {reading.value:.3f}, "
            f"чистых пикселей {reading.valid_fraction*100:.0f}%")
