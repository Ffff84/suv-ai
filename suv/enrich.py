"""
Подключение спутника к конкретному полю.

Отдельный модуль, потому что и расчёт, и бот должны обогащать поле
одинаково. Раньше fetch_ndvi вызывался только в диагностике: спутник
формально работал, но на рекомендацию не влиял вообще — самый неприятный
вид дыры, потому что снаружи всё выглядит рабочим.

Правило: обогащение НИКОГДА не роняет расчёт. Нет ключей, нет сети,
сплошная облачность — движок продолжает считать по календарю культуры и
честно сообщает, что спутника не было.

Источников теперь два, и порядок между ними не случаен. Sentinel-2 идёт
первым всегда: 10 метров дают на саду 2,52 га около 250 пикселей против
двух с половиной десятков у Landsat, и отставание данных у него сутки
против девяти дней.

Landsat подключается ТОЛЬКО когда Sentinel-2 не дал годного кадра. За
прошлый год это 20 дат, которые иначе движок отработал бы вслепую по
календарю: 49 годных дат против 69 вместе (замер воспроизводится
scripts/landsat_revisit.py). Худшие провалы второй источник при этом
почти не лечит — многонедельную облачность не пробивает ни один
оптический сенсор, — так что его польза в плотности записи, а не в
страховке от молчания.

Смешивать два ряда в один тренд нельзя (см. оговорку в suv/landsat.py),
поэтому запасной источник и работает как заплата на дыру, а не как
равноправный поставщик.
"""

from __future__ import annotations

import os
from datetime import date

from . import landsat
from .satellite import bbox_polygon, fetch_ndvi, get_token

# Окно поиска для Landsat. Пара 8/9 проходит над полем раз в восемь дней,
# и поле лежит на стыке рядов WRS-2 — за две недели набирается до четырёх
# кадров. Меньше брать нельзя: одного прохода может не хватить, а
# blended_kc всё равно обнуляет вес снимка к четырнадцатому дню.
LANDSAT_WINDOW_DAYS = 14


def _calendar_tail(status: str) -> str:
    """Дописать «считаю по календарю», если этого ещё не сказано.

    Строка статуса уходит в лог бота и в вывод scripts/run_field.py, и
    из неё должно быть однозначно видно, работал движок по снимку или по
    календарю культуры. Дублировать фразу, которая уже есть в тексте,
    не надо — читать такое невозможно.
    """
    return status if "календар" in status else f"{status} — считаю по календарю"


def _sentinel2(fld, poly: list, window_days: int) -> str:
    """Основной источник. Возвращает строку статуса, поле трогает только
    при успехе."""
    if not os.environ.get("CDSE_CLIENT_ID") or not os.environ.get("CDSE_CLIENT_SECRET"):
        # Без хвоста про календарь: его допишет _calendar_tail, и только
        # если запасной источник тоже ничего не дал. Иначе строка
        # сообщала бы «считаю по календарю» и тут же рапортовала о
        # снимке Landsat — прямое противоречие внутри одной записи лога.
        return "спутник не подключён"

    try:
        token = get_token()
    except Exception as exc:  # noqa: BLE001
        return f"спутник: ошибка авторизации ({type(exc).__name__})"

    try:
        reading = fetch_ndvi(poly, token, window_days=window_days)
    except Exception as exc:  # noqa: BLE001
        return f"спутник: снимок не получен ({type(exc).__name__})"

    if reading is None:
        return f"спутник: за {window_days} дн. все снимки в облаках"

    fld.ndvi = reading.value
    fld.ndvi_date = reading.observed_on
    return (f"Sentinel-2: NDVI {reading.value:.3f}, "
            f"чистых пикселей {reading.valid_fraction*100:.0f}%")


def _landsat(fld, poly: list, window_days: int, before: str) -> str:
    """Запасной источник. Зовётся, только если Sentinel-2 ничего не дал."""
    try:
        reading = landsat.fetch_ndvi(
            poly, window_days=max(window_days, LANDSAT_WINDOW_DAYS))
    except Exception as exc:  # noqa: BLE001 — правило модуля: не ронять расчёт
        return _calendar_tail(f"{before}; Landsat не ответил "
                              f"({type(exc).__name__})")

    if reading is None:
        return _calendar_tail(f"{before}; у Landsat тоже нет годного кадра")

    fld.ndvi = reading.value
    fld.ndvi_date = reading.observed_on
    # Возраст кадра в строку обязателен: у Landsat отставание около девяти
    # дней, и blended_kc обнулит вес такого снимка к четырнадцатому. Без
    # даты в логе «спутник сработал» читалось бы как «свежий снимок есть».
    age = (date.today() - reading.observed_on).days
    return (f"{before}; Landsat: NDVI {reading.value:.3f}, "
            f"кадр {reading.observed_on.isoformat()} ({age} дн. назад), "
            f"чистых пикселей {reading.valid_fraction*100:.0f}%")


def attach_ndvi(fld, field_size_m: float = 200.0,
                window_days: int = 12, polygon: list | None = None) -> str:
    """
    Дописать в поле свежий NDVI. Возвращает строку статуса для лога.

    polygon — реальные границы участка (GeoJSON ring, [[lon,lat],...]).
    Если их нет, берём квадрат вокруг точки: для пилота этого достаточно,
    но квадрат может захватить соседнюю культуру или дорогу, поэтому
    настоящие границы всегда лучше.
    """
    poly = polygon or bbox_polygon(fld.lat, fld.lon, field_size_m)

    status = _sentinel2(fld, poly, window_days)
    if fld.ndvi is not None:
        return status
    if not landsat.enabled():
        return _calendar_tail(status)
    return _landsat(fld, poly, window_days, status)
