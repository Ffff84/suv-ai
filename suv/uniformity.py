"""
Равномерность полива по многолетнему композиту NDVI.

Вопрос секции «Sug'orish tekisligi» — «доходит ли вода до конца поля» —
до сих пор честно висел в NO_DATA: рисовать заливку по нормативам
запрещено (§1.1), а замера не было. Замер появился, и он спутниковый:

    зоны продуктивности OneSoil, повёрнутые под наш вопрос. Не «где
    сеять гуще», а «есть ли у поля стабильно сухой дальний край вдоль
    хода воды». Один сезон так не скажет (пятно может быть градом или
    вредителем); 3-6 сезонов подряд — уже почерк арыка.

Три честных числа рядом с каждым выводом (перенято у их отчёта):
сколько сезонов вошло, какая доля площади стабильна, насколько дальний
край отстаёт от среднего. Порог применимости объявляется вслух: меньше
трёх сезонов или стабильной площади меньше 40% — «карту не строю»,
а не догадка красивым градиентом.

Сетевые вызовы — только в fetch_grid/build; вся математика чистая и
живёт под тестами без сети.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import requests

from .clock import today as today_tashkent
from .satellite import EVALSCRIPT, PROCESS_URL, get_token

# Пороги применимости — те же, что OneSoil публикует для своих зон
# (стабильность >= 3 сезонов и > 40% площади): чужие, но разумные и
# главное — объявленные, а не подкрученные под красивый ответ.
MIN_SEASONS = 3
MIN_STABLE_SHARE = 0.40

# Классы относительного NDVI: ниже 0,93 от среднего по полю — «низкий»,
# выше 1,07 — «высокий». Пиксель стабилен, если держит класс в >= 70%
# вошедших сезонов.
LOW_T, HIGH_T = 0.93, 1.07
STABLE_FRAC = 0.70

# Композит собирается по пику сезона: полог сомкнут, полив в разгаре.
SEASON_FROM = (6, 15)
SEASON_TO = (8, 15)
SCENES_PER_SEASON = 3

SLICES = 8              # ломтики вдоль хода воды
TAIL_OK_PCT = -7.0      # дальний край не хуже -7% — вода доходит
TAIL_ALERT_PCT = -15.0

GRID = 64


# ------------------------------------------------------------ сеть (тонко)

def fetch_grid(polygon: list[list[float]], day: date, token: str):
    """Растр NDVI одного дня по полигону: (ndvi, clear, inside) как numpy.
    Тот же EVALSCRIPT, что кормит рекомендацию, — никакой второй правды."""
    import numpy as np  # noqa: F401
    import rasterio
    from io import BytesIO

    body = {
        "input": {
            "bounds": {"geometry": {"type": "Polygon",
                                    "coordinates": [polygon]}},
            "data": [{
                "type": "sentinel-2-l2a",
                "dataFilter": {
                    "timeRange": {"from": f"{day.isoformat()}T00:00:00Z",
                                  "to": f"{day.isoformat()}T23:59:59Z"}},
            }],
        },
        "output": {"width": GRID, "height": GRID,
                   "responses": [{"identifier": "default",
                                  "format": {"type": "image/tiff"}}]},
        "evalscript": EVALSCRIPT,
    }
    r = requests.post(PROCESS_URL, json=body, timeout=60,
                      headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    with rasterio.open(BytesIO(r.content)) as src:
        ndvi = src.read(1).astype("float32")
        clear = src.read(3).astype("float32") > 0
        inside = src.read(4).astype("float32") > 0
    return ndvi, clear, inside


# ------------------------------------------------------- чистая математика

def season_mean(grids: list) -> tuple:
    """Средний NDVI пикселя за сезон по годным кадрам.

    Каждый элемент — (ndvi, clear); пиксель усредняется только по
    кадрам, где он чист. Возвращает (grid, известность-маска)."""
    import numpy as np
    acc = None
    cnt = None
    for ndvi, clear in grids:
        v = np.where(clear, ndvi, 0.0)
        acc = v if acc is None else acc + v
        cnt = clear.astype("int16") if cnt is None else cnt + clear
    if acc is None:
        return None, None
    known = cnt > 0
    mean = np.divide(acc, np.maximum(cnt, 1), where=True)
    return mean, known


def relative_grid(grid, known, inside):
    """NDVI пикселя относительно среднего по полю этого же сезона.

    Относительная шкала — сознательно: абсолютный NDVI пляшет от года к
    году с погодой, а вопрос «какой край хуже СВОЕГО поля» — нет."""
    import numpy as np
    mask = known & inside
    if not mask.any():
        return None
    field_mean = float(grid[mask].mean())
    if field_mean <= 0:
        return None
    rel = np.where(mask, grid / field_mean, np.nan)
    return rel


def classify(rel):
    """-1 низкий / 0 средний / +1 высокий; NaN -> -128 (неизвестно)."""
    import numpy as np
    cls = np.full(rel.shape, -128, dtype="int8")
    ok = ~np.isnan(rel)
    cls[ok & (rel < LOW_T)] = -1
    cls[ok & (rel > HIGH_T)] = 1
    cls[ok & (rel >= LOW_T) & (rel <= HIGH_T)] = 0
    return cls


def stability(class_stack, inside):
    """Доля стабильных пикселей и маска стабильно низких.

    Пиксель стабилен, когда держит один класс в >= STABLE_FRAC доле
    сезонов, где он был виден."""
    import numpy as np
    stack = np.stack(class_stack)                    # (S, H, W)
    known = stack != -128
    seen = known.sum(axis=0)
    stable = np.zeros(inside.shape, dtype=bool)
    stable_low = np.zeros(inside.shape, dtype=bool)
    for value in (-1, 0, 1):
        hits = ((stack == value) & known).sum(axis=0)
        is_stable = (seen > 0) & (hits >= np.ceil(STABLE_FRAC * seen))
        stable |= is_stable
        if value == -1:
            stable_low = is_stable
    denom = int((inside & (seen > 0)).sum())
    if denom == 0:
        return 0.0, stable_low
    share = float((stable & inside & (seen > 0)).sum()) / denom
    return share, stable_low


def reach_profile(rel_mean, inside, ring: list[list[float]],
                  flow_bearing_deg: float, slices: int = SLICES):
    """Средний относительный NDVI по ломтикам вдоль хода воды.

    Пиксели проецируются на ось хода (растр лежит в bbox полигона),
    ломтик 0 — у входа, последний — дальний край. tail_pct — насколько
    дальние два ломтика отстают от ближних двух, в процентах."""
    import numpy as np
    lons = [p[0] for p in ring]
    lats = [p[1] for p in ring]
    lon0, lon1 = min(lons), max(lons)
    lat0, lat1 = min(lats), max(lats)
    h, w = rel_mean.shape
    # Центры пикселей: строка 0 — север (верх растра).
    xs = np.linspace(lon0, lon1, w, endpoint=False) + (lon1 - lon0) / w / 2
    ys = np.linspace(lat1, lat0, h, endpoint=False) - (lat1 - lat0) / h / 2
    m_lat = 111_320.0
    m_lon = m_lat * max(0.2, math.cos(math.radians((lat0 + lat1) / 2)))
    X = (xs[None, :] - lon0) * m_lon * np.ones((h, 1))
    Y = (ys[:, None] - lat0) * m_lat * np.ones((1, w))

    b = math.radians(flow_bearing_deg)
    t = X * math.sin(b) + Y * math.cos(b)            # позиция вдоль хода

    ok = inside & ~np.isnan(rel_mean)
    if ok.sum() < slices * 4:
        return None, None
    t0, t1 = float(t[ok].min()), float(t[ok].max())
    if t1 - t0 <= 0:
        return None, None
    idx = np.clip(((t - t0) / (t1 - t0) * slices).astype(int), 0, slices - 1)
    means = []
    for s in range(slices):
        sel = ok & (idx == s)
        means.append(float(rel_mean[sel].mean()) if sel.any() else float("nan"))
    head = [m for m in means[:2] if not math.isnan(m)]
    tail = [m for m in means[-2:] if not math.isnan(m)]
    if not head or not tail:
        return means, None
    tail_pct = (sum(tail) / len(tail) / (sum(head) / len(head)) - 1.0) * 100.0
    return means, round(tail_pct, 1)


def compose(season_rels: list, inside):
    """Свести сезонные относительные растры: композит + три честных числа."""
    import numpy as np
    usable = [r for r in season_rels if r is not None]
    if not usable:
        return None
    stack = np.stack(usable)
    rel_mean = np.nanmean(stack, axis=0)
    share, stable_low = stability([classify(r) for r in usable], inside)
    return {"rel_mean": rel_mean, "stable_share": round(share, 3),
            "stable_low": stable_low, "seasons_used": len(usable)}


# ----------------------------------------------------------------- сборка

def build(polygon: list[list[float]], flow_bearing_deg: float,
          flow_source: str, years: int = 6,
          token: str | None = None) -> dict:
    """Собрать замер равномерности поля за `years` последних сезонов.

    Возвращает словарь для хранения в журнале. При недостатке данных —
    честный отказ с причиной, а не карта-догадка."""
    from .indices import scene_days
    token = token or get_token()
    today = today_tashkent()

    season_rels = []
    scenes_used = 0
    inside_ref = None
    for year in range(today.year - years, today.year + 1):
        a = date(year, *SEASON_FROM)
        b = date(year, *SEASON_TO)
        if a > today:
            continue
        days = scene_days(polygon, token, a, min(b, today))
        if not days:
            continue
        picks = sorted(days)
        if len(picks) > SCENES_PER_SEASON:
            step = (len(picks) - 1) / (SCENES_PER_SEASON - 1)
            picks = [picks[round(i * step)] for i in range(SCENES_PER_SEASON)]
        grids = []
        for d in picks:
            try:
                ndvi, clear, inside = fetch_grid(polygon, d, token)
            except Exception:  # noqa: BLE001 — один кадр не топит сезон
                continue
            frac = (clear & inside).sum() / max(int(inside.sum()), 1)
            if frac < 0.30:
                continue
            inside_ref = inside
            grids.append((ndvi, clear))
            scenes_used += 1
        mean, known = season_mean(grids)
        if mean is None:
            season_rels.append(None)
            continue
        season_rels.append(relative_grid(mean, known, inside_ref))

    payload: dict = {"built": today.isoformat(),
                     "flow_bearing_deg": round(flow_bearing_deg, 1),
                     "flow_source": flow_source,
                     "scenes_used": scenes_used}
    got = compose(season_rels, inside_ref) if inside_ref is not None else None
    if got is None or got["seasons_used"] < MIN_SEASONS:
        payload.update(seasons_used=(got or {}).get("seasons_used", 0),
                       stable_share=(got or {}).get("stable_share", 0.0),
                       refused="few_seasons", tail_pct=None, slice_means=None)
        return payload
    if got["stable_share"] < MIN_STABLE_SHARE:
        payload.update(seasons_used=got["seasons_used"],
                       stable_share=got["stable_share"],
                       refused="unstable", tail_pct=None, slice_means=None)
        return payload

    means, tail_pct = reach_profile(got["rel_mean"], inside_ref, polygon,
                                    flow_bearing_deg)
    payload.update(
        seasons_used=got["seasons_used"], stable_share=got["stable_share"],
        refused=None if tail_pct is not None else "too_small",
        tail_pct=tail_pct,
        slice_means=[None if m is None or (isinstance(m, float) and math.isnan(m))
                     else round(m, 3) for m in (means or [])] or None)
    return payload
