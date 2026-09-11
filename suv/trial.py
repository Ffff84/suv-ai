"""
Контрольная полоса по-нашему: половина поля по совету, половина по привычке.

У OneSoil контрольные полосы — главное, что стоило забрать: их VRA-опыт
отвечает «сработала ли норма» урожаем с комбайна. Комбайна с датчиком у
нашего фермера нет, а спутник есть — поэтому наш опыт другой:

    поле делится на две половины ВДОЛЬ хода воды, «A» поливается по
    совету бота, «B» — как фермер привык, а разницу половин раз в
    3–5 дней меряет Sentinel-2 (NDMI/NDVI по каждой половине отдельно).

Это лекарство от «эталона нет»: не абсолютная урожайность, а парное
сравнение на одном поле, одной почве и одной погоде. Выводы из рядов
делает человек — модуль только делит поле и честно меряет.

Почему деление именно вдоль хода воды: борозды идут от входа воды, и
обе половины обязаны получать воду независимо. Линия раздела поперёк
хода отрезала бы половину «B» от арыка — опыт превратился бы в засуху.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .field_shape import area_ha as ring_area_ha
from .field_shape import from_geojson_ring

M_PER_DEG_LAT = 111_320.0

# Половина не может быть меньше этой доли поля: сильно кривой контур
# делится «осколком», и сравнивать 0,2 га с 2 га бессмысленно.
MIN_HALF_FRAC = 0.25


@dataclass
class TrialSplit:
    half_a: list[list[float]]     # кольцо GeoJSON, слева по ходу воды
    half_b: list[list[float]]     # справа по ходу воды
    area_a: float
    area_b: float
    flow_bearing_deg: float       # куда течёт вода (азимут от севера)


def _local(pts_latlon):
    lat0 = sum(p[0] for p in pts_latlon) / len(pts_latlon)
    lon0 = sum(p[1] for p in pts_latlon) / len(pts_latlon)
    m_lon = M_PER_DEG_LAT * max(0.2, math.cos(math.radians(lat0)))
    xy = [((lon - lon0) * m_lon, (lat - lat0) * M_PER_DEG_LAT)
          for lat, lon in pts_latlon]
    return xy, (lat0, lon0, m_lon)


def _unlocal(xy, origin):
    lat0, lon0, m_lon = origin
    return [[round(lon0 + x / m_lon, 6), round(lat0 + y / M_PER_DEG_LAT, 6)]
            for x, y in xy]


def _clip_halfplane(poly, c, n, keep_negative):
    """Sutherland–Hodgman об одну прямую через c с нормалью n."""
    def side(p):
        v = (p[0] - c[0]) * n[0] + (p[1] - c[1]) * n[1]
        return -v if keep_negative else v

    out = []
    m = len(poly)
    for i in range(m):
        p, q = poly[i], poly[(i + 1) % m]
        sp, sq = side(p), side(q)
        if sp >= 0:
            out.append(p)
        if (sp > 0) != (sq > 0) and sp != sq:
            t = sp / (sp - sq)
            out.append((p[0] + t * (q[0] - p[0]), p[1] + t * (q[1] - p[1])))
    return out


def _long_axis_bearing(xy) -> float:
    """Азимут длинной оси контура — заменитель хода воды, когда вход не
    отмечен: борозды обычно кладут вдоль длинной стороны."""
    best, bearing = -1.0, 0.0
    m = len(xy)
    for i in range(m):
        x1, y1 = xy[i]
        x2, y2 = xy[(i + 1) % m]
        d2 = (x2 - x1) ** 2 + (y2 - y1) ** 2
        if d2 > best:
            best = d2
            bearing = math.degrees(math.atan2(x2 - x1, y2 - y1)) % 360
    return bearing


def default_flow_bearing(ring: list[list[float]]) -> float:
    """Ход воды, когда вход не отмечен: вдоль длинной оси контура."""
    pts = from_geojson_ring(ring)
    xy, _ = _local(pts)
    return round(_long_axis_bearing(xy), 1)


def flow_bearing_from_inlet(ring: list[list[float]], i: int, j: int) -> float:
    """Куда течёт вода: перпендикуляр к ребру входа, В поле (к центроиду)."""
    pts = from_geojson_ring(ring)
    xy, _ = _local(pts)
    n = len(xy)
    p, q = xy[i % n], xy[j % n]
    mx, my = (p[0] + q[0]) / 2, (p[1] + q[1]) / 2
    cx = sum(x for x, _ in xy) / n
    cy = sum(y for _, y in xy) / n
    return math.degrees(math.atan2(cx - mx, cy - my)) % 360


def split(ring: list[list[float]],
          flow_bearing_deg: float | None = None) -> TrialSplit:
    """Разделить контур на две половины вдоль хода воды.

    Линия раздела — через центроид, параллельно ходу воды: обе половины
    сохраняют доступ к входу. Без известного хода берётся длинная ось
    контура (об этом честно говорит вернувшийся bearing).
    """
    pts = from_geojson_ring(ring)
    if len(pts) < 3:
        raise ValueError("контур из менее чем трёх вершин")
    xy, origin = _local(pts)
    if flow_bearing_deg is None:
        flow_bearing_deg = _long_axis_bearing(xy)

    b = math.radians(flow_bearing_deg)
    d = (math.sin(b), math.cos(b))          # направление хода воды
    n_vec = (-d[1], d[0])                   # нормаль: делит лево/право
    cx = sum(x for x, _ in xy) / len(xy)
    cy = sum(y for _, y in xy) / len(xy)

    left = _clip_halfplane(xy, (cx, cy), n_vec, keep_negative=False)
    right = _clip_halfplane(xy, (cx, cy), n_vec, keep_negative=True)
    if len(left) < 3 or len(right) < 3:
        raise ValueError("контур не делится этой осью — задайте ход воды "
                         "вручную (--bearing)")

    def close(r):
        ring_ll = _unlocal(r, origin)
        if ring_ll[0] != ring_ll[-1]:
            ring_ll.append(list(ring_ll[0]))
        return ring_ll

    ha_l = ring_area_ha([(la, lo) for lo, la in close(left)[:-1]])
    ha_r = ring_area_ha([(la, lo) for lo, la in close(right)[:-1]])
    total = ha_l + ha_r
    if total <= 0 or min(ha_l, ha_r) < MIN_HALF_FRAC * total:
        raise ValueError(
            f"половины слишком неравные ({ha_l:.2f} / {ha_r:.2f} га) — "
            "контур кривой для этой оси, задайте --bearing")

    return TrialSplit(half_a=close(left), half_b=close(right),
                      area_a=round(ha_l, 2), area_b=round(ha_r, 2),
                      flow_bearing_deg=round(flow_bearing_deg, 1))


# ------------------------------------------------------------------ отчёт

@dataclass
class TrialRow:
    day: object
    ndvi_a: float
    ndvi_b: float
    ndmi_a: float
    ndmi_b: float
    clear: float                  # худшая из половин

    @property
    def d_ndvi(self) -> float:
        return self.ndvi_a - self.ndvi_b

    @property
    def d_ndmi(self) -> float:
        return self.ndmi_a - self.ndmi_b


def pair_readings(a_list, b_list) -> list[TrialRow]:
    """Совместить ряды половин по дням: сравнивать можно только кадры
    одного дня — половины из разных дат сравнивают погоду, не полив."""
    b_by_day = {r.day: r for r in b_list}
    rows = []
    for a in a_list:
        b = b_by_day.get(a.day)
        if b is None:
            continue
        rows.append(TrialRow(day=a.day, ndvi_a=a.ndvi, ndvi_b=b.ndvi,
                             ndmi_a=a.ndmi, ndmi_b=b.ndmi,
                             clear=min(a.valid_fraction, b.valid_fraction)))
    return rows


def summarize(rows: list[TrialRow]) -> dict:
    """Средние разницы половин. Числа, не вывод: вывод делает человек."""
    if not rows:
        return {"days": 0}
    return {
        "days": len(rows),
        "mean_d_ndvi": round(sum(r.d_ndvi for r in rows) / len(rows), 4),
        "mean_d_ndmi": round(sum(r.d_ndmi for r in rows) / len(rows), 4),
        "last_d_ndmi": round(rows[-1].d_ndmi, 4),
    }
