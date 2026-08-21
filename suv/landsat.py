"""
Landsat 8/9 через Microsoft Planetary Computer — запасной глаз.

Зачем отдельный источник — и чего он НЕ даёт. Замер каталогов над садом
Фарруха за 12 месяцев (21.08.2025–21.08.2026, порог облачности 20%,
воспроизводится `scripts/landsat_revisit.py --field fields/olma.json`):

    Sentinel-2          88 проходов, 49 годных, худший провал 30 дней
    Landsat 8/9         43 прохода,  29 годных, худший провал 48 дней
    вместе                          69 годных, худший провал 30 дней

То есть Landsat добавляет 20 годных дат в год, которых у Sentinel-2 нет
вовсе, — плотность записи растёт на 41%. А вот ПРОВАЛЫ он почти не
лечит: за год худший разрыв как был 30 дней, так и остался, в сезоне
(01.04–21.08.2026) сокращается всего с 15 дней до 14. Причина простая и
её надо называть вслух: самые длинные провалы — это многонедельная
облачность, сквозь которую не видит ни один оптический сенсор, а Landsat
именно оптический. Обещать им «бот не замолчит зимой» нельзя; честное
обещание — «между снимками Sentinel-2 станет вдвое меньше пустых дней».

Числа получены ЗАМЕРОМ КАТАЛОГА, а не расчётом внутри проекта, и скрипт
рядом позволяет их пересчитать на любую дату и оспорить.

Почему Planetary Computer, а не CDSE. Landsat есть и в Copernicus Data
Space, подключается теми же ключами — но там лежит только Level-1 TOA,
несовместимый по радиометрии с нашим L2A, и данные отстают на 89 дней
(на 21.08.2026 последняя дата — 24 мая). Для бота, который советует
полив сегодня, это мёртвый архив. Planetary Computer отдаёт Level-2
surface reflectance с отставанием около девяти дней, анонимно, без
регистрации и без ключей.

Роль строго вспомогательная: Landsat включается ТОЛЬКО когда Sentinel-2
не дал годного кадра. Причина в физике. Пиксель 30 м — это 900 м², и на
саду 2,52 га внутрь контура попадает порядка десятка пикселей, на
винограднике 1,5 га — единицы. Среднее по полю из этого посчитать можно,
карту неоднородности — нельзя, поэтому фото фермеру остаётся на
Sentinel-2 и только на нём.

Честная оговорка, которую нельзя терять: NDVI Landsat и NDVI Sentinel-2
систематически расходятся (разная ширина каналов, разный угол обзора).
Опубликованные коэффициенты сшивки (Claverie et al., RSE 2018) здесь
СОЗНАТЕЛЬНО не применяются — на наших полях они не проверены, а
подгонять чужой поправкой измерение, которое сам не сверял, значит
завести тихую неправду. Вместо этого источник кадра попадает в строку
статуса, чтобы расхождение было видно, а не замазано.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import date, timedelta

import requests

from .satellite import NdviReading

log = logging.getLogger("suv.landsat")

STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
SAS_URL = ("https://planetarycomputer.microsoft.com/api/sas/v1/token/"
           "landsat-c2-l2")
COLLECTION = "landsat-c2-l2"

# Паспортные коэффициенты Landsat Collection 2 Level-2. Нужны как запасной
# вариант: у части гранул scale и offset не прописаны в самом GeoTIFF, и
# rasterio вернёт scale=1.0, offset=0.0. Без явного пересчёта отражение
# уезжает вдвое — проверено на живом кадре, NDVI выходил 0.239 вместо
# 0.472.
SR_SCALE, SR_OFFSET = 2.75e-05, -0.2

# Биты qa_pixel, при которых пиксель непригоден. Список сверен с DFCB
# Landsat 8/9 Collection 2, а не взят из статьи.
QA_FILL = 0
QA_DILATED_CLOUD = 1
QA_CIRRUS = 2
QA_CLOUD = 3
QA_CLOUD_SHADOW = 4
QA_SNOW = 5
_QA_BAD_BITS = (QA_FILL, QA_DILATED_CLOUD, QA_CIRRUS, QA_CLOUD,
                QA_CLOUD_SHADOW, QA_SNOW)

# Пары битов уверенности: значение 2 — «средняя», 3 — «высокая».
# Однобитные флаги выше CFMask поднимает ТОЛЬКО при высокой уверенности,
# поэтому одной их проверки мало: пиксель под тонким облаком или летней
# пылевой дымкой проходит как чистый, занижает NDVI, и движок откладывает
# полив на сохнущем саду. Берём в брак и среднюю уверенность: на поле в
# два гектара дешевле пропустить сомнительный кадр, чем полить не вовремя.
QA_CLOUD_CONFIDENCE = 8
QA_SHADOW_CONFIDENCE = 10
_QA_CONFIDENCE_BITS = (QA_CLOUD_CONFIDENCE, QA_SHADOW_CONFIDENCE)
_QA_CONFIDENCE_BAD = 2

# Доля чистых пикселей ВНУТРИ КОНТУРА, при которой кадру можно верить.
# Знаменатель — площадь поля, а не прямоугольника кадра (ТЗ §4.2, тот же
# смысл, что у valid_fraction в suv/field_photo.py:stats_over_field).
MIN_VALID_FRACTION = 0.30

# Меньше четырёх пикселей внутри контура — среднее считать не из чего.
# Виноградник 1,5 га при пикселе 30 м даёт около четырёх целых пикселей,
# и это уже нижняя граница осмысленного; ниже честнее промолчать, чем
# выдать число, за которым стоит один-два замера.
MIN_PIXELS = 4

# Ниже этой суммы отражений (nir + red) поверхность практически чёрная —
# глубокая тень, вода, залитая борозда, — и NDVI там не измерение, а шум
# деления малого на малое. Порог обязателен именно у Landsat: после
# сдвига -0.2 отражение легально бывает отрицательным, и знаменатель
# проходит через ноль. Пара DN 7000 и 7546 даёт знаменатель 1.5e-05 и
# NDVI около тысячи; отсечь такое «не равно нулю» невозможно, а зажать
# в единицу — значит объявить чёрный пиксель максимумом растительности,
# то есть ошибиться в другую сторону. Поэтому пиксель выбрасывается.
MIN_REFLECTANCE_SUM = 0.05

# Сколько кадров подряд пробуем, прежде чем сдаться. Пара Landsat 8/9
# проходит над полем раз в 8 дней, причём поле лежит на стыке рядов
# WRS-2 032/033 и на каждую дату приходит по две сцены. Четыре попытки —
# это около двух недель назад, дальше кадр всё равно старше окна, в
# котором blended_kc даёт снимку хоть какой-то вес.
MAX_TRIES = 4

# Общий потолок времени на весь вызов. Он здесь главнее покадровых
# таймаутов: GDAL_HTTP_TIMEOUT ограничивает ОДИН range-запрос, а чтение
# COG по сети делает их несколько на канал, да ещё с ретраями, так что
# верхней границы у операции без этого потолка нет вообще. А _compute_rec
# зовётся СИНХРОННО внутри async-хендлеров бота (bot/main.py:558 и
# :1887): зависший обход подвесил бы утреннюю рассылку целиком.
MAX_SECONDS = 25.0

# Настройки GDAL для чтения COG по сети. READDIR_ON_OPEN гасит попытку
# перечислить «каталог» рядом с файлом — по HTTP это лишний круг запросов
# на каждый канал.
_GDAL_OPTS = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "GDAL_HTTP_CONNECTTIMEOUT": "10",
    "GDAL_HTTP_TIMEOUT": "20",
    "GDAL_HTTP_MAX_RETRY": "1",
    "GDAL_HTTP_RETRY_DELAY": "1",
}

_token: tuple[str, float] | None = None  # (значение, момент протухания)


def enabled() -> bool:
    """Включён ли запасной источник.

    Пусто = выключено, и выкатка этой ветки ничего не меняет для
    пилотного фермера: та же рекомендация из того же Sentinel-2.
    Включается строкой LANDSAT_FALLBACK=1 в .env — осознанным действием,
    а не самим фактом деплоя. Тот же приём, что у FIELD_STATUS_CHAT_IDS.
    """
    return os.environ.get("LANDSAT_FALLBACK", "").strip().lower() in (
        "1", "true", "yes", "on")


def _sas_token() -> str:
    """Ключ доступа к хранилищу. Выдаётся анонимно и живёт около часа."""
    global _token
    now = time.monotonic()
    if _token is not None and now < _token[1]:
        return _token[0]
    r = requests.get(SAS_URL, timeout=20)
    r.raise_for_status()
    # Полчаса про запас: токен живёт час, но утренний обход по всем полям
    # не должен упереться в протухание на середине списка.
    _token = (r.json()["token"], now + 30 * 60)
    return _token[0]


def search_scenes(ring: list[list[float]], today: date | None = None,
                  window_days: int = 14) -> list[dict]:
    """Сцены над полем, от свежей к старой.

    Облачность сцены НЕ фильтруем — сознательно, по той же причине, что и
    в suv/scene.py: облако в сорока километрах от поля выбраковывает
    совершенно годный кадр. Годность решается по доле чистых пикселей
    ВНУТРИ контура, а это видно только когда кадр уже прочитан.
    """
    today = today or date.today()
    start = today - timedelta(days=window_days)
    body = {
        "collections": [COLLECTION],
        "intersects": {"type": "Polygon", "coordinates": [ring]},
        "datetime": (f"{start.isoformat()}T00:00:00Z/"
                     f"{today.isoformat()}T23:59:59Z"),
        "limit": 20,
    }
    r = requests.post(STAC_URL, json=body, timeout=30)
    r.raise_for_status()
    feats = r.json().get("features", [])
    feats.sort(key=lambda f: f["properties"]["datetime"], reverse=True)
    return feats


def _scale_of(asset: dict) -> tuple[float, float]:
    """Множитель и сдвиг из метаданных STAC, иначе паспортные."""
    bands = asset.get("raster:bands") or []
    if isinstance(bands, list) and bands:
        b = bands[0]
        if "scale" in b and "offset" in b:
            return float(b["scale"]), float(b["offset"])
    return SR_SCALE, SR_OFFSET


def bad_quality(qa):
    """Маска непригодных пикселей по qa_pixel.

    Вынесена отдельно, потому что это единственное место, где решается,
    какой пиксель попадёт в среднее, и его надо уметь проверять тестом
    без сети.
    """
    import numpy as np

    bad = np.zeros(qa.shape, dtype=bool)
    for bit in _QA_BAD_BITS:
        bad |= ((qa >> bit) & 1).astype(bool)
    for bit in _QA_CONFIDENCE_BITS:
        bad |= (((qa >> bit) & 3) >= _QA_CONFIDENCE_BAD)
    return bad


def mean_ndvi(red_dn, nir_dn, usable, red_scale=SR_SCALE, red_offset=SR_OFFSET,
              nir_scale=SR_SCALE, nir_offset=SR_OFFSET):
    """Средний NDVI по пригодным пикселям. None — считать не из чего.

    Два предохранителя, оба не декоративные. Первый: пиксель с почти
    чёрной поверхностью выбрасывается по MIN_REFLECTANCE_SUM, а не
    «проверяется на ноль». Отбросить по точному равенству нулю нельзя —
    знаменатель у такого пикселя равен 1.5e-05, а не нулю; зажать
    результат в единицу тоже нельзя — это объявило бы тень максимумом
    растительности. Один такой пиксель из двадцати пяти уводил бы среднее
    в десятки. Второй предохранитель: результат всё равно зажимается в
    [-1, 1] — за этими границами NDVI не определён, и любое значение
    оттуда есть след деления малого на малое, а не измерение.

    Без них kc_from_ndvi упирался бы в границу конверта культуры, и
    фермер получал бы команду лить в полтора раза больше воды, чем нужно.
    """
    import numpy as np

    red = red_dn.astype("float64") * red_scale + red_offset
    nir = nir_dn.astype("float64") * nir_scale + nir_offset
    denom = nir + red
    ok = usable & (denom > MIN_REFLECTANCE_SUM)
    if not ok.any():
        return None
    ndvi = np.clip((nir[ok] - red[ok]) / denom[ok], -1.0, 1.0)
    return float(ndvi.mean())


def _read_scene(feat: dict, ring: list[list[float]]) -> tuple | None:
    """(ndvi, доля чистых от площади поля). None — считать не из чего."""
    import numpy as np
    import rasterio
    from rasterio.features import geometry_mask
    from rasterio.warp import transform as warp_transform
    from rasterio.windows import from_bounds

    epsg = feat["properties"].get("proj:epsg")
    if not epsg:
        log.warning("landsat: у сцены %s нет proj:epsg", feat.get("id"))
        return None
    xs, ys = warp_transform("EPSG:4326", f"EPSG:{epsg}",
                            [p[0] for p in ring], [p[1] for p in ring])
    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)
    geom = {"type": "Polygon", "coordinates": [list(zip(xs, ys))]}

    token = _sas_token()
    out = {}
    inside = None
    with rasterio.Env(**_GDAL_OPTS):
        for name in ("red", "nir08", "qa_pixel"):
            asset = feat["assets"].get(name)
            if asset is None:
                log.warning("landsat: у сцены %s нет канала %s",
                            feat.get("id"), name)
                return None
            with rasterio.open(f"{asset['href']}?{token}") as src:
                w = from_bounds(minx, miny, maxx, maxy, src.transform)
                # boundless: поле может частично выйти за край сцены на
                # стыке рядов 032/033. Пустое добивается нулём, а ноль у
                # Landsat и означает «данных нет».
                arr = src.read(1, window=w, boundless=True, fill_value=0)
                if inside is None:
                    # Контур растеризуется В ТО ЖЕ ОКНО. Без этого среднее
                    # считалось бы по описанному прямоугольнику: поля
                    # Фарруха стоят в 86 м друг от друга, то есть в трёх
                    # пикселях Landsat, и прямоугольник сада на капле
                    # захватывал бы виноградник на самотёке, дорогу и
                    # канал между ними (ТЗ §4.2).
                    inside = geometry_mask(
                        [geom], out_shape=arr.shape,
                        transform=src.window_transform(w), invert=True)
            out[name] = np.asarray(arr)

    qa = out["qa_pixel"]
    if qa.size == 0 or inside is None:
        return None

    in_field = int(inside.sum())
    if in_field < MIN_PIXELS:
        log.info("landsat: внутри контура %d пикселей, нужно %d — молчу",
                 in_field, MIN_PIXELS)
        return None

    red_dn, nir_dn = out["red"], out["nir08"]
    # Ноль — заполнитель, а не измерение: за краем сцены и без данных.
    no_data = (red_dn == 0) | (nir_dn == 0)
    usable = inside & ~bad_quality(qa) & ~no_data
    # Знаменатель — площадь ПОЛЯ, а не кадра. Иначе доля мерила бы, какую
    # часть прямоугольника занимает участок (ТЗ §4.2).
    frac = float(usable.sum()) / in_field
    if usable.sum() < MIN_PIXELS:
        return None

    rs, ro = _scale_of(feat["assets"]["red"])
    ns, no = _scale_of(feat["assets"]["nir08"])
    ndvi = mean_ndvi(red_dn, nir_dn, usable, rs, ro, ns, no)
    if ndvi is None:
        return None
    return ndvi, frac


def fetch_ndvi(ring: list[list[float]], today: date | None = None,
               window_days: int = 14) -> NdviReading | None:
    """Средний NDVI по контуру поля с ближайшего годного кадра Landsat.

    Кадры перебираются от свежего к старому, берётся ПЕРВЫЙ, у которого
    доля чистых пикселей внутри контура достаточна. Это тот же порядок, в
    котором собирается фото поля (bot/main.py), и он же — главное отличие
    от пути Sentinel-2, где мозаика mostRecent показывает ровно одну самую
    свежую сцену: облачная — и расчёт откатывается на календарь, хотя
    тремя днями раньше лежит чистый кадр.

    None означает «годного кадра нет» — вызывающий обязан считать по
    календарю культуры, а не подставлять последнее известное значение.
    """
    today = today or date.today()
    started = time.monotonic()
    try:
        feats = search_scenes(ring, today, window_days)
    except Exception as exc:  # noqa: BLE001
        log.warning("landsat: каталог не ответил (%s: %s)",
                    type(exc).__name__, exc)
        return None
    if not feats:
        log.info("landsat: за %d дн. над полем не было ни одной сцены",
                 window_days)
        return None

    for feat in feats[:MAX_TRIES]:
        if time.monotonic() - started > MAX_SECONDS:
            log.warning("landsat: потрачено больше %.0f с — прекращаю перебор",
                        MAX_SECONDS)
            break
        try:
            got = _read_scene(feat, ring)
        except Exception as exc:  # noqa: BLE001 — одна битая сцена не решает
            # Молчать здесь нельзя: без этой строки мёртвый источник и
            # честная облачность выглядят в логе одинаково.
            log.warning("landsat: сцена %s не прочиталась (%s: %s)",
                        feat.get("id"), type(exc).__name__, exc)
            continue
        if got is None:
            continue
        ndvi, frac = got
        if frac < MIN_VALID_FRACTION:
            continue
        day = date.fromisoformat(feat["properties"]["datetime"][:10])
        return NdviReading(value=ndvi, observed_on=day, valid_fraction=frac)
    return None
