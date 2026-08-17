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

# ---------------------------------------------------------------- стороны

# Румбы, как их называет фермер. Восьми хватает: «вода заходит с
# северо-северо-востока» — не та точность, которой кто-нибудь оперирует
# на поле.
COMPASS_UZ = ("shimol", "shimoli-sharq", "sharq", "janubi-sharq",
              "janub", "janubi-g'arb", "g'arb", "shimoli-g'arb")
COMPASS_RU = ("север", "северо-восток", "восток", "юго-восток",
              "юг", "юго-запад", "запад", "северо-запад")


def compass_index(bearing_deg: float) -> int:
    """Румб по азимуту: 0 — север, дальше по часовой стрелке."""
    return int((bearing_deg % 360.0 + 22.5) // 45.0) % 8


def compass_name(bearing_deg: float, lang: str = "uz") -> str:
    names = COMPASS_UZ if lang == "uz" else COMPASS_RU
    return names[compass_index(bearing_deg)]


@dataclass(frozen=True)
class Edge:
    """Сторона поля: цепочка меж от вершины i до вершины j.

    i и j — индексы вершин контура, как требует ТЗ §2.3. j не обязан
    быть следующим за i: настоящая межа редко бывает одним отрезком, и
    северную сторону, обойденную семью точками вдоль кривой границы,
    фермер всё равно называет северной.
    """

    i: int
    j: int
    bearing: float     # азимут ВНЕШНЕЙ нормали: куда сторона смотрит из поля
    length_m: float    # длина по прямой от вершины i до вершины j

    def name(self, lang: str = "uz") -> str:
        return compass_name(self.bearing, lang)


def _signed_area_m2(xy: list[tuple[float, float]]) -> float:
    """Площадь со знаком: положительная — обход против часовой стрелки."""
    s = 0.0
    for i in range(len(xy)):
        x1, y1 = xy[i]
        x2, y2 = xy[(i + 1) % len(xy)]
        s += x1 * y2 - x2 * y1
    return s / 2.0


# Насколько межа вправе вилять, оставаясь одной стороной поля: предел
# отклонения её точек от прямой, доля длины этой стороны.
#
# Углы для этого не годятся, хотя так и напрашивается. Граница, идущая
# зигзагом вдоль соседской межи, состоит из отрезков, смотрящих на
# северо-запад и северо-восток — между ними 109°, больше прямого угла,
# хотя это одна северная сторона. А настоящий угол поля — ровно 90°.
# По углу между отрезками эти два случая неразличимы; по отклонению от
# прямой — да: у виляющей межи оно около 9% длины, у завёрнутого угла 45%.
#
# Доля берётся от РАЗМЕРА ПОЛЯ (четверти периметра — это примерно одна
# его сторона), а не от длины уже собранного куска. Иначе цепочка не
# успевает начаться: на первых двух отрезках хорда коротка, допуск с неё
# — считанные метры, и виляющая межа рвётся, не начав склеиваться.
SIDE_DEVIATION_FRAC = 0.25
# Поле бывает маленьким, а телефон врёт на метры: на короткой стороне
# доля превращается в сантиметры, и любое дрожание рвёт её на куски.
SIDE_DEVIATION_MIN_M = 8.0


def _angle_gap(a: float, b: float) -> float:
    """Разница двух азимутов, 0..180."""
    d = abs(a - b) % 360.0
    return min(d, 360.0 - d)


def edges(points: list[tuple[float, float]]) -> list[Edge]:
    """Стороны поля со сторонами света, от самой длинной к короткой.

    Сторона — это направление ВНЕШНЕЙ НОРМАЛИ: куда межа смотрит наружу
    поля. Азимут от центра к середине ребра для этого не годится: на
    Г-образном участке вдоль канала центр лежит в вырезе, и нижняя межа
    получает имя «юго-восточная», хотя она южная.

    Соседние отрезки, смотрящие примерно в одну сторону, склеиваются в
    ОДНУ сторону. Без этого северная граница, обойденная семью точками
    вдоль кривой межи, рассыпается на «северо-западные» и
    «северо-восточные» куски, и севера в списке нет вообще — фермер его
    ищет и не находит. А поле, обведённое двадцатью точками, давало
    двадцать почти одинаковых кнопок.

    Направление обхода фермер выбирает сам, поэтому его определяем по
    знаку площади и разворачиваем нормаль куда надо.

    Длинные стороны идут первыми: вода заходит с канала или дороги, а
    они тянутся вдоль длинной межи, а не упираются в угол.
    """
    n = len(points)
    if n < 3:
        return []
    xy = to_local_m(points)
    ccw = _signed_area_m2(xy) > 0

    def outward(ax: int, bx: int) -> tuple[float, float]:
        """(азимут внешней нормали, длина) для хорды от вершины ax к bx."""
        dx = xy[bx][0] - xy[ax][0]
        dy = xy[bx][1] - xy[ax][1]
        # При обходе против часовой стрелки поле слева от направления
        # движения, значит наружу смотрит (dy, -dx). По часовой — наоборот.
        nx, ny = (dy, -dx) if ccw else (-dy, dx)
        return math.degrees(math.atan2(nx, ny)) % 360.0, math.hypot(dx, dy)

    seg = [outward(i, (i + 1) % n)[0] for i in range(n)]

    # Начинаем с настоящего угла — там, где направление меняется резче
    # всего. Иначе цепочка разорвётся посреди ровной межи только потому,
    # что фермер начал обход именно с неё.
    start = max(range(n), key=lambda i: _angle_gap(seg[i], seg[i - 1]))

    def deviation(a: int, b: int, count: int) -> float:
        """Насколько промежуточные вершины отходят от прямой a-b."""
        ax, ay = xy[a]
        bx, by = xy[b]
        dx, dy = bx - ax, by - ay
        span = math.hypot(dx, dy)
        if span < 1e-9:
            return float("inf")   # цепочка вернулась в начало — не сторона
        worst = 0.0
        for t in range(1, count):
            px, py = xy[(a + t) % n]
            worst = max(worst, abs(dx * (py - ay) - dy * (px - ax)) / span)
        return worst

    perimeter = sum(math.hypot(xy[(i + 1) % n][0] - xy[i][0],
                               xy[(i + 1) % n][1] - xy[i][1]) for i in range(n))
    allowed = max(SIDE_DEVIATION_MIN_M, SIDE_DEVIATION_FRAC * perimeter / 4.0)

    out: list[Edge] = []
    k = 0
    while k < n:
        i = (start + k) % n
        run_end = (i + 1) % n
        length = 1
        while length < n - k:
            cand_end = (i + length + 1) % n
            if deviation(i, cand_end, length + 1) > allowed:
                break
            run_end = cand_end
            length += 1
        bearing, span = outward(i, run_end)
        out.append(Edge(i=i, j=run_end, bearing=bearing, length_m=span))
        k += length
    return sorted(out, key=lambda e: -e.length_m)


def side_labels(points: list[tuple[float, float]],
                lang: str = "uz") -> list[tuple[Edge, str]]:
    """Стороны поля с ПОДПИСЯМИ, различимыми между собой.

    У поля с вырезом две северные межи бывают одинаковой длины, и обе
    подписи получаются буквально одинаковыми — фермер выбирает наугад, а
    ошибку потом не видно: карточка в обоих случаях скажет «вода заходит
    с севера». Поэтому совпавшие подписи уточняются положением стороны
    на поле: «shimol · 200 m (g'arbroq)».
    """
    uz = lang == "uz"
    sides = edges(points)
    if not sides:
        return []
    xy = to_local_m(points)

    def base(e: Edge) -> str:
        return (f"{e.name(lang)} tomondan · {e.length_m:.0f} m" if uz
                else f"с {e.name(lang)}а · {e.length_m:.0f} м")

    def where(e: Edge) -> str:
        """Куда смещена середина стороны относительно центра поля."""
        n = len(points)
        mx = (xy[e.i][0] + xy[e.j % n][0]) / 2.0
        my = (xy[e.i][1] + xy[e.j % n][1]) / 2.0
        bearing = math.degrees(math.atan2(mx, my)) % 360.0
        name = compass_name(bearing, lang)
        return f"{name}roq" if uz else f"{name} часть"

    labels = [base(e) for e in sides]
    seen: dict[str, int] = {}
    for lbl in labels:
        seen[lbl] = seen.get(lbl, 0) + 1
    out: list[tuple[Edge, str]] = []
    used: set[str] = set()
    for e, lbl in zip(sides, labels):
        if seen[lbl] > 1:
            lbl = f"{lbl} ({where(e)})"
        if lbl in used:  # осталось совпадение — доводим номером
            k = 2
            while f"{lbl} ({k})" in used:
                k += 1
            lbl = f"{lbl} ({k})"
        used.add(lbl)
        out.append((e, lbl))
    return out


def inlet_edge(points: list[tuple[float, float]],
               inlet: tuple[int, int] | None) -> Edge | None:
    """Ребро входа воды по сохранённым индексам вершин.

    Сначала ищем среди склеенных сторон — их предлагает интерфейс. Если
    пара не нашлась, но это соседние вершины, строим ребро по сырому
    отрезку. Ворота полива часто и есть короткий торец: на настоящем
    винограднике пилота вход с северо-востока — межа в 33 метра, которую
    склейка честно прячет внутри длинной «восточной» стороны. Точное
    ребро, записанное агрономом, важнее списка из восьми кнопок.
    """
    if not inlet:
        return None
    n = len(points)
    i, j = inlet
    if not (0 <= i < n and 0 <= j < n):
        return None
    found = next((e for e in edges(points) if (e.i, e.j) == (i, j)), None)
    if found is not None or j != (i + 1) % n:
        return found
    xy = to_local_m(points)
    ccw = _signed_area_m2(xy) > 0
    dx = xy[j][0] - xy[i][0]
    dy = xy[j][1] - xy[i][1]
    nx, ny = (dy, -dx) if ccw else (-dy, dx)
    return Edge(i=i, j=j,
                bearing=math.degrees(math.atan2(nx, ny)) % 360.0,
                length_m=math.hypot(dx, dy))


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
