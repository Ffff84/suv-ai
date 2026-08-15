"""
Контур поля: площадь, проверка, сборка полигона из углов.

Чистая геометрия, ноль зависимостей от telegram и от сети. ТЗ предлагает
`shapely.is_valid`, но самопересечение двадцати рёбер проверяется сорока
строками, а shapely — это компилируемый GEOS, который на пилотном VPS
может не встать за два дня до дедлайна. Тот же довод, по которому в
проекте нет python-dotenv.

Все расчёты — в локальной метрической проекции вокруг центра поля. Для
участка в сотни метров ошибка такой проекции сильно меньше процента, а
ТЗ требует сходимости площади с документами в пределах 5%.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Пределы из ТЗ §2.1. Меньше трёх вершин — не многоугольник; больше
# двадцати фермер пальцем не рисует, это обводка со спутника.
MIN_VERTICES = 3
MAX_VERTICES = 20
MIN_AREA_HA = 0.1
MAX_AREA_HA = 500.0
# Поле не может оказаться в другой области: почти всегда это отправленная
# из дома геолокация вместо угла поля.
MAX_DRIFT_KM = 50.0

EARTH_R_KM = 6371.0088


def meters_per_degree(lat_deg: float) -> tuple[float, float]:
    """(метров в градусе широты, метров в градусе долготы) на этой широте.

    WGS84, разложение по косинусам. Считать по «111 320 везде» нельзя:
    на широте Самарканда градус долготы короче градуса широты на четверть,
    и площадь поля уезжает на десятки процентов.
    """
    phi = math.radians(lat_deg)
    m_lat = (111_132.92 - 559.82 * math.cos(2 * phi)
             + 1.175 * math.cos(4 * phi) - 0.0023 * math.cos(6 * phi))
    m_lon = (111_412.84 * math.cos(phi) - 93.5 * math.cos(3 * phi)
             + 0.118 * math.cos(5 * phi))
    return m_lat, m_lon


def centroid(points: list[tuple[float, float]]) -> tuple[float, float]:
    """Среднее по вершинам (lat, lon). Не центр масс многоугольника —
    для упорядочивания углов и выбора начала координат этого достаточно."""
    return (sum(p[0] for p in points) / len(points),
            sum(p[1] for p in points) / len(points))


def to_local_m(points: list[tuple[float, float]],
               origin: tuple[float, float] | None = None) -> list[tuple[float, float]]:
    """(lat, lon) -> (x, y) в метрах от центра поля. x на восток, y на север."""
    lat0, lon0 = origin or centroid(points)
    m_lat, m_lon = meters_per_degree(lat0)
    return [((lon - lon0) * m_lon, (lat - lat0) * m_lat) for lat, lon in points]


def distance_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Haversine. Нужен только для проверки «не уехало ли поле»."""
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * EARTH_R_KM * math.asin(min(1.0, math.sqrt(h)))


def area_ha(points: list[tuple[float, float]]) -> float:
    """Площадь замкнутого контура в гектарах. Формула шнурков по модулю:
    направление обхода фермера нас не касается."""
    if len(points) < 3:
        return 0.0
    xy = to_local_m(points)
    s = 0.0
    for i in range(len(xy)):
        x1, y1 = xy[i]
        x2, y2 = xy[(i + 1) % len(xy)]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0 / 10_000.0


# ---------------------------------------------------------------- простота

def _orientation(p, q, r) -> int:
    """Знак векторного произведения: 1 — против часовой, -1 — по, 0 — на одной прямой."""
    v = (q[1] - p[1]) * (r[0] - q[0]) - (q[0] - p[0]) * (r[1] - q[1])
    if abs(v) < 1e-9:
        return 0
    return 1 if v > 0 else -1


def _on_segment(p, q, r) -> bool:
    """q лежит на отрезке pr (при условии, что все три коллинеарны)."""
    return (min(p[0], r[0]) - 1e-9 <= q[0] <= max(p[0], r[0]) + 1e-9
            and min(p[1], r[1]) - 1e-9 <= q[1] <= max(p[1], r[1]) + 1e-9)


def _segments_cross(p1, p2, p3, p4) -> bool:
    o1, o2 = _orientation(p1, p2, p3), _orientation(p1, p2, p4)
    o3, o4 = _orientation(p3, p4, p1), _orientation(p3, p4, p2)
    if o1 != o2 and o3 != o4:
        return True
    # Коллинеарные наложения — тоже самопересечение: контур ложится
    # сам на себя, и заливка вдоль борозды по нему не строится.
    if o1 == 0 and _on_segment(p1, p3, p2):
        return True
    if o2 == 0 and _on_segment(p1, p4, p2):
        return True
    if o3 == 0 and _on_segment(p3, p1, p4):
        return True
    if o4 == 0 and _on_segment(p3, p2, p4):
        return True
    return False


def has_duplicate_points(points: list[tuple[float, float]]) -> bool:
    """Одна и та же точка отмечена дважды. Отдельно от самопересечения:
    фермеру надо сказать «этот угол уже есть», а не «контур пересёкся» —
    второе не подсказывает, что делать."""
    xy = to_local_m(points)
    return len({(round(x, 3), round(y, 3)) for x, y in xy}) < len(points)


def is_simple(points: list[tuple[float, float]]) -> bool:
    """Контур не пересекает сам себя — замена shapely.is_valid.

    Двадцать вершин дают 190 пар рёбер: полный перебор здесь дешевле
    любой зависимости.
    """
    n = len(points)
    if n < 3:
        return False
    if has_duplicate_points(points):
        return False
    xy = to_local_m(points)
    for i in range(n):
        a1, a2 = xy[i], xy[(i + 1) % n]
        for j in range(i + 1, n):
            if j == i or (j + 1) % n == i or (i + 1) % n == j:
                continue  # соседние рёбра делят вершину, это не пересечение
            b1, b2 = xy[j], xy[(j + 1) % n]
            if _segments_cross(a1, a2, b1, b2):
                return False
    return True


def convex_hull_order(points: list[tuple[float, float]]) -> list[tuple[float, float]] | None:
    """Обход по выпуклой оболочке — или None, если она берёт не все точки.

    Здесь важна не сама оболочка, а её следствие: если ВСЕ вершины лежат
    на выпуклой оболочке, то простой многоугольник через них ровно один,
    и порядок обхода не выбирается, а вычисляется. Тогда развернуть
    присланные вразнобой углы — не догадка, а единственный ответ.

    Если хоть одна точка лежит внутри оболочки, поле невыпуклое, простых
    контуров через этот набор много, и выбирать между ними мы не вправе:
    у Г-образного участка вдоль канала «правдоподобный» контур съедает
    вырез и завышает площадь в разы. Такой набор возвращает None, и
    вызывающий обязан переспросить фермера, а не изобретать поле.

    Монотонная цепочка Эндрю; коллинеарные точки СОХРАНЯЮТСЯ — лишняя
    отметка посреди прямой стороны не должна выглядеть как невыпуклость.
    """
    n = len(points)
    if n < 3:
        return None
    xy = to_local_m(points)
    idx = sorted(range(n), key=lambda i: (xy[i][0], xy[i][1]))

    def build(order):
        chain: list[int] = []
        for i in order:
            # < 0 — строгий правый поворот: точки на прямой остаются.
            while len(chain) >= 2 and _orientation(
                    xy[chain[-2]], xy[chain[-1]], xy[i]) < 0:
                chain.pop()
            chain.append(i)
        return chain

    lower = build(idx)
    upper = build(list(reversed(idx)))
    hull = lower[:-1] + upper[:-1]
    if len(hull) != n or len(set(hull)) != n:
        return None
    return [points[i] for i in hull]


# ---------------------------------------------------------------- проверка

@dataclass
class ShapeCheck:
    ok: bool
    error: str | None       # ключ для i18n, не готовый текст
    area: float             # гектары; 0.0, если контур не сложился
    points: list[tuple[float, float]]


def validate(points: list[tuple[float, float]],
             anchor: tuple[float, float] | None = None,
             *, reorder: bool = False) -> ShapeCheck:
    """Проверить контур по правилам ТЗ §2.1 и вернуть площадь.

    anchor — ранее известная точка поля. Контур, чей центр оказался за
    50 км от неё, почти наверняка нарисован не там.

    Порядок вершин — это ДАННЫЕ, а не шум: фермер обходит поле по
    периметру, и присланная последовательность и есть его граница. Мы её
    не «чиним».

    reorder разрешает единственное исправление — и только то, которое
    доказуемо: если все углы лежат на выпуклой оболочке, простой контур
    через них ровно один, и восстановить его можно без догадок. Во всех
    остальных случаях (Г-образный участок вдоль канала, серп, вырез под
    постройку) простых контуров через тот же набор точек много, они
    отличаются по площади в разы — и выбрать за фермера значит показать
    ему поле, которого он не обходил. Тогда честнее переспросить.

    ТЗ §2.2 предлагает всегда сортировать вершины по азимуту от центра.
    Так делать нельзя: для Г-образного поля 5,6 га эта сортировка
    съедает вырез и уверенно сообщает 13,6 га, а проверка на
    самопересечение такую подмену поймать не может — контур, собранный
    по возрастанию угла, самопересекающимся не бывает никогда.
    """
    pts = [p for p in points]
    if len(pts) < MIN_VERTICES:
        return ShapeCheck(False, "too_few", 0.0, pts)
    if len(pts) > MAX_VERTICES:
        return ShapeCheck(False, "too_many", 0.0, pts)
    if has_duplicate_points(pts):
        return ShapeCheck(False, "duplicate_point", 0.0, pts)

    if not is_simple(pts):
        hull = convex_hull_order(pts) if reorder else None
        if hull is None:
            return ShapeCheck(False, "order_unclear" if reorder
                              else "self_intersects", 0.0, pts)
        pts = hull

    if anchor is not None and distance_km(centroid(pts), anchor) > MAX_DRIFT_KM:
        return ShapeCheck(False, "too_far", 0.0, pts)

    ha = area_ha(pts)
    if ha < MIN_AREA_HA:
        return ShapeCheck(False, "too_small", ha, pts)
    if ha > MAX_AREA_HA:
        return ShapeCheck(False, "too_big", ha, pts)
    return ShapeCheck(True, None, ha, pts)


# ---------------------------------------------------------------- хранение

def to_geojson_ring(points: list[tuple[float, float]]) -> list[list[float]]:
    """(lat, lon) -> замкнутое кольцо [[lon, lat], ...].

    Порядок lon/lat — не каприз: именно его ждёт Copernicus в
    satellite.fetch_ndvi, и перепутанные оси дают снимок Индийского
    океана вместо Самаркандской области.
    """
    ring = [[lon, lat] for lat, lon in points]
    if ring[0] != ring[-1]:
        ring.append(ring[0])
    return ring


def from_geojson_ring(ring: list[list[float]]) -> list[tuple[float, float]]:
    """Обратно в (lat, lon), без замыкающей повторной вершины."""
    pts = [(lat, lon) for lon, lat in ring]
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts.pop()
    return pts
