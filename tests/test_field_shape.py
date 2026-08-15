"""
Контур поля: площадь и проверка.

Эталон площади взят прямоугольником с известными сторонами — так ошибку
в проекции видно сразу, а не через расхождение с документами фермера на
пилоте. ТЗ §7 требует именно этой проверки, плюс отклонения
самопересекающегося контура.
"""

from __future__ import annotations

import math

import pytest

from suv.field_shape import (MAX_DRIFT_KM, area_ha, centroid,
                             convex_hull_order, distance_km,
                             from_geojson_ring, has_duplicate_points,
                             is_simple, to_geojson_ring, validate)

# Самарканд: широта, на которой работает пилот. Градус долготы здесь
# заметно короче градуса широты — ошибка «111 320 везде» тут и вылезает.
LAT0, LON0 = 39.6547, 66.9597


def rectangle(width_m: float, height_m: float,
              lat: float = LAT0, lon: float = LON0):
    """Прямоугольник заданных размеров в метрах, (lat, lon) по углам."""
    from suv.field_shape import meters_per_degree
    m_lat, m_lon = meters_per_degree(lat)
    dlat = height_m / 2 / m_lat
    dlon = width_m / 2 / m_lon
    return [(lat - dlat, lon - dlon), (lat - dlat, lon + dlon),
            (lat + dlat, lon + dlon), (lat + dlat, lon - dlon)]


# ---------------------------------------------------------------- площадь

def _reference_area_ha(points):
    """Независимый эталон: площадь на сфере через формулу Жирара.

    Другой алгоритм и другие константы, чем в area_ha. Считать эталон
    тем же meters_per_degree, что и проверяемый код, бессмысленно:
    прямоугольник, построенный делением на ту же константу, сойдётся
    при ЛЮБОМ её значении, и ошибка в проекции останется невидимой.
    """
    import math
    r = 6_371_007.2  # радиус равновеликой сферы, м
    s = 0.0
    for i in range(len(points)):
        lat1, lon1 = points[i]
        lat2, lon2 = points[(i + 1) % len(points)]
        s += (math.radians(lon2 - lon1)
              * (2 + math.sin(math.radians(lat1)) + math.sin(math.radians(lat2))))
    return abs(s * r * r / 2.0) / 10_000.0


def test_area_matches_an_independent_geodesic_reference():
    """Проекционная площадь сходится со сферической формулой.

    Два независимых алгоритма на одних координатах — это и есть проверка
    проекции: перепутанные широта с долготой, потерянный косинус или
    множитель 2 разъедут результаты в разы.

    Допуск 1%, и он не может быть меньше: эталон считает по сфере, а
    meters_per_degree — по эллипсоиду WGS84, и на широте 10° это законно
    расходится на 0,4%. Требовать большего значило бы проверять не
    правильность, а совпадение двух разных моделей Земли. Для ТЗ, где
    площадь должна сойтись с документами в пределах 5%, запаса хватает.
    """
    for w, h, lat in ((100, 100, LAT0), (500, 200, LAT0), (800, 600, 10.0),
                      (300, 300, 55.0), (400, 250, -33.0)):
        pts = rectangle(w, h, lat=lat)
        assert area_ha(pts) == pytest.approx(_reference_area_ha(pts), rel=1e-2)


def test_area_matches_a_hardcoded_field_of_known_size():
    """Эталон, не зависящий ни от одной функции модуля: контур задан
    градусами напрямую, площадь посчитана вне проекта.

    Прямоугольник 0,002° широты x 0,002° долготы на 39,6547° с.ш.
    По WGS84 это 222,05 м x 171,58 м = 3,8100 га.
    """
    d = 0.001
    pts = [(LAT0 - d, LON0 - d), (LAT0 - d, LON0 + d),
           (LAT0 + d, LON0 + d), (LAT0 + d, LON0 - d)]
    assert area_ha(pts) == pytest.approx(3.810, rel=3e-3)


def test_area_of_a_ten_hectare_field():
    """500 x 200 м = 10 га — размер настоящего поля из ТЗ."""
    assert area_ha(rectangle(500, 200)) == pytest.approx(10.0, rel=1e-3)


def test_area_is_independent_of_winding_direction():
    """Фермер обходит поле как ему удобно; площадь не может быть
    отрицательной или разной."""
    rect = rectangle(300, 150)
    assert area_ha(rect) == pytest.approx(area_ha(list(reversed(rect))))


def test_area_stays_accurate_at_a_different_latitude():
    """Проекция считается по широте поля, а не по одной зашитой цифре."""
    for lat in (10.0, 39.65, 55.0):
        assert area_ha(rectangle(200, 200, lat=lat)) == pytest.approx(4.0,
                                                                      rel=2e-3)


def test_triangle_area():
    """Половина прямоугольника — половина площади."""
    a, b, c, _d = rectangle(400, 200)
    assert area_ha([a, b, c]) == pytest.approx(4.0, rel=1e-3)


# ---------------------------------------------------------------- простота

def test_self_intersecting_polygon_is_rejected():
    """«Бабочка»: вершины переставлены так, что рёбра пересекаются."""
    a, b, c, d = rectangle(200, 200)
    bowtie = [a, b, d, c]  # диагональный перехлёст
    assert not is_simple(bowtie)
    assert validate(bowtie).error == "self_intersects"


def test_plain_rectangle_is_simple():
    assert is_simple(rectangle(200, 200))


def test_repeated_vertex_is_reported_as_a_duplicate_not_a_crossing():
    """«Контур пересёкся» на дважды отмеченном углу — совет, по которому
    нечего сделать. Фермеру надо сказать, что этот угол уже есть."""
    a, b, c, d = rectangle(200, 200)
    assert has_duplicate_points([a, b, b, c, d])
    assert not is_simple([a, b, b, c, d])
    assert validate([a, b, b, c, d], reorder=True).error == "duplicate_point"


def test_scrambled_corners_of_a_convex_field_are_restored():
    """Все углы на выпуклой оболочке — простой контур через них ровно
    один, поэтому порядок не угадывается, а вычисляется."""
    a, b, c, d = rectangle(300, 300)
    res = validate([a, c, b, d], reorder=True)
    assert res.ok, res.error
    assert res.area == pytest.approx(9.0, rel=1e-3)
    assert is_simple(res.points)


def test_drawn_polygon_is_never_silently_reordered():
    """Контур из Mini App фермер чертит пальцем: порядок вершин там
    осмысленный. Нарисованную «бабочку» отклоняем, а не выпрямляем —
    иначе бот покажет площадь поля, которого никто не рисовал."""
    a, b, c, d = rectangle(200, 200)
    bowtie = [a, b, d, c]
    assert validate(bowtie).error == "self_intersects"
    assert validate(bowtie, reorder=True).ok  # выпуклый набор — чинится


# ---------------------------------------------- невыпуклые поля: не гадать

def _l_shaped():
    """Г-образный участок вдоль канала: 600x60 м вдоль воды плюс отвод
    60x340 м. Обычная форма, а не выдуманная патология."""
    from suv.field_shape import meters_per_degree
    m_lat, m_lon = meters_per_degree(LAT0)
    def P(x, y): return (LAT0 + y / m_lat, LON0 + x / m_lon)
    return [P(0, 0), P(600, 0), P(600, 60), P(60, 60), P(60, 400), P(0, 400)]


def test_walked_perimeter_of_a_concave_field_is_taken_as_is():
    """Обход по периметру — это данные, а не шум: любую форму, пришедшую
    в порядке обхода, принимаем как есть."""
    res = validate(_l_shaped(), reorder=True)
    assert res.ok
    assert res.area == pytest.approx(5.64, rel=1e-2)


def test_scrambled_corners_of_a_concave_field_are_refused_not_invented():
    """Ключевое правило. Через невыпуклый набор точек простых контуров
    много, и они отличаются по площади в разы. Сортировка по азимуту
    (её предлагает ТЗ) съедает вырез и уверенно сообщает 13,6 га вместо
    5,6 — а проверить это нечем: контур, собранный по возрастанию угла,
    самопересекающимся не бывает никогда. Поэтому — переспросить.
    """
    field = _l_shaped()
    scrambled = [field[i] for i in (0, 1, 2, 3, 5, 4)]
    assert not is_simple(scrambled)
    res = validate(scrambled, reorder=True)
    assert not res.ok
    assert res.error == "order_unclear"
    assert res.area == 0.0


def test_no_permutation_of_a_concave_field_yields_an_invented_ring():
    """Ни одна перестановка не должна дать принятый контур, которого
    фермер не обходил.

    Принять можно только порядок, который САМ по себе даёт простой
    контур: тогда мы сообщаем ровно ту фигуру, которую нам прислали.
    Всё остальное — отказ. Раньше сортировка по азимуту принимала 660
    перестановок из 720, и 48 из них с площадью, завышенной вдвое.
    """
    import itertools
    field = _l_shaped()
    invented = 0
    for perm in itertools.permutations(field):
        pts = list(perm)
        if validate(pts, reorder=True).ok and not is_simple(pts):
            invented += 1
    assert invented == 0


def test_accepted_rings_are_always_exactly_what_was_sent():
    """Принятый контур совпадает с присланным по порядку вершин с
    точностью до поворота — бот не переставляет углы молча."""
    import itertools
    field = _l_shaped()
    for perm in itertools.permutations(field):
        res = validate(list(perm), reorder=True)
        if res.ok:
            assert res.points == list(perm), "порядок вершин изменён"


def test_convex_hull_order_refuses_non_convex_sets():
    """Оболочка возвращает порядок, только если на ней лежат ВСЕ точки:
    иначе восстановление контура — догадка."""
    assert convex_hull_order(rectangle(200, 200)) is not None
    assert convex_hull_order(_l_shaped()) is None


def test_convex_hull_order_keeps_a_point_on_a_straight_edge():
    """Отметка посреди прямой стороны — не признак невыпуклости:
    фермер вправе отметить лишнюю точку на длинной меже."""
    from suv.field_shape import meters_per_degree
    m_lat, m_lon = meters_per_degree(LAT0)
    def P(x, y): return (LAT0 + y / m_lat, LON0 + x / m_lon)
    with_midpoint = [P(0, 0), P(200, 0), P(400, 0), P(400, 250), P(0, 250)]
    hull = convex_hull_order(with_midpoint)
    assert hull is not None and len(hull) == 5
    assert area_ha(hull) == pytest.approx(10.0, rel=1e-3)


# ---------------------------------------------------------------- проверка

def test_too_few_and_too_many_vertices():
    a, b, _c, _d = rectangle(200, 200)
    assert validate([a, b]).error == "too_few"

    # 21 вершина по окружности — больше предела ТЗ.
    from suv.field_shape import meters_per_degree
    m_lat, m_lon = meters_per_degree(LAT0)
    ring = [(LAT0 + 100 * math.sin(2 * math.pi * i / 21) / m_lat,
             LON0 + 100 * math.cos(2 * math.pi * i / 21) / m_lon)
            for i in range(21)]
    assert validate(ring).error == "too_many"


def test_area_bounds():
    assert validate(rectangle(20, 20)).error == "too_small"      # 0,04 га
    assert validate(rectangle(3000, 3000)).error == "too_big"    # 900 га


def test_polygon_far_from_the_known_field_is_rejected():
    """Геолокация из дома вместо угла поля — самая частая ошибка."""
    far = rectangle(200, 200, lat=41.3, lon=69.2)  # Ташкент
    res = validate(far, anchor=(LAT0, LON0))
    assert res.error == "too_far"
    assert distance_km(centroid(far), (LAT0, LON0)) > MAX_DRIFT_KM

    near = rectangle(200, 200)
    assert validate(near, anchor=(LAT0, LON0)).ok


def test_valid_field_reports_its_area():
    res = validate(rectangle(400, 250))
    assert res.ok and res.error is None
    assert res.area == pytest.approx(10.0, rel=1e-3)


# ---------------------------------------------------------- стороны света

def test_edges_of_a_rectangle_get_the_right_compass_sides():
    """Поле 400 м по долготе и 200 м по широте: длинные межи — северная
    и южная, короткие — восточная и западная."""
    from suv.field_shape import edges, meters_per_degree
    m_lat, m_lon = meters_per_degree(LAT0)
    def P(x, y): return (LAT0 + y / m_lat, LON0 + x / m_lon)
    rect = [P(0, 0), P(400, 0), P(400, 200), P(0, 200)]

    got = {e.name("uz"): round(e.length_m) for e in edges(rect)}
    assert got == {"janub": 400, "shimol": 400, "sharq": 200, "g'arb": 200}


def test_sides_of_a_concave_field_are_named_by_the_outward_normal():
    """Г-образный участок вдоль канала. Азимут от центра тут врёт: центр
    лежит в вырезе, и нижняя межа получает название «юго-восточная»,
    хотя она южная — фермер ищет в списке юг и не находит. Сторона
    определяется тем, куда межа смотрит наружу поля."""
    from suv.field_shape import edges
    got = {e.name("uz") for e in edges(_l_shaped())}
    assert got == {"janub", "shimol", "sharq", "g'arb"}


def test_sides_do_not_depend_on_the_walking_direction():
    """Фермер обходит поле по часовой или против — стороны света от
    этого не меняются.

    Длины сравнивать нельзя: цепочки при обратном обходе могут
    захватить короткий отвод на метр-другой иначе. Фермер видит стороны
    света, и они обязаны совпадать.
    """
    from suv.field_shape import edges
    field = _l_shaped()
    forward = sorted(e.name("uz") for e in edges(field))
    backward = sorted(e.name("uz") for e in edges(list(reversed(field))))
    assert forward == backward


def test_a_side_is_a_run_of_segments_not_a_single_edge():
    """Северная граница, обойденная семью точками вдоль кривой межи, —
    это одна северная сторона, а не шесть «северо-западных» и
    «северо-восточных» кусков, среди которых севера нет вовсе."""
    from suv.field_shape import edges, meters_per_degree
    m_lat, m_lon = meters_per_degree(LAT0)
    def P(x, y): return (LAT0 + y / m_lat, LON0 + x / m_lon)
    jagged = ([P(0, 0), P(400, 0), P(400, 250)]
              + [P(400 * k / 8, 250 + (35 if k % 2 else -35))
                 for k in range(7, 0, -1)]
              + [P(0, 250)])
    sides = edges(jagged)
    assert any(e.name("uz") == "shimol" for e in sides), \
        "северной стороны нет в списке — фермер её не найдёт"
    assert len(sides) < 8, f"граница рассыпалась на {len(sides)} кусков"


def test_sides_cover_the_whole_boundary_exactly_once():
    """Цепочки идут подряд и замыкают контур: ни дыр, ни нахлёстов."""
    from suv.field_shape import edges, meters_per_degree
    import math
    m_lat, m_lon = meters_per_degree(LAT0)
    def P(x, y): return (LAT0 + y / m_lat, LON0 + x / m_lon)
    shapes = [
        rectangle(400, 250),
        _l_shaped(),
        [P(200 * math.cos(math.radians(a * 18)),
           200 * math.sin(math.radians(a * 18))) for a in range(20)],
    ]
    for pts in shapes:
        n = len(pts)
        covered = sum((e.j - e.i) % n for e in edges(pts))
        assert covered == n, f"покрытие {covered} из {n}"


def test_side_labels_are_always_distinguishable():
    """У поля с вырезом две северные межи бывают одной длины: подписи
    совпадали буквально, фермер выбирал наугад, а ошибку потом не
    видно — карточка в обоих случаях говорит «вода заходит с севера»."""
    from suv.field_shape import meters_per_degree, side_labels
    m_lat, m_lon = meters_per_degree(LAT0)
    def P(x, y): return (LAT0 + y / m_lat, LON0 + x / m_lon)
    notched = [P(0, 0), P(400, 0), P(400, 200), P(200, 200),
               P(200, 500), P(0, 500)]
    labels = [lbl for _e, lbl in side_labels(notched)]
    assert len(labels) == len(set(labels)), labels
    assert sum(1 for l in labels if l.startswith("shimol")) == 2

    ru = [lbl for _e, lbl in side_labels(notched, "ru")]
    assert len(ru) == len(set(ru))


def test_edges_are_sorted_longest_first():
    """Вода заходит с канала, а он тянется вдоль длинной межи — её и
    показываем фермеру первой."""
    from suv.field_shape import edges, meters_per_degree
    m_lat, m_lon = meters_per_degree(LAT0)
    def P(x, y): return (LAT0 + y / m_lat, LON0 + x / m_lon)
    lengths = [e.length_m for e in edges([P(0, 0), P(600, 0), P(600, 60),
                                          P(0, 60)])]
    assert lengths == sorted(lengths, reverse=True)


def test_compass_sectors_split_at_the_half_rumb():
    from suv.field_shape import compass_name
    assert compass_name(0) == "shimol" and compass_name(359) == "shimol"
    assert compass_name(22) == "shimol"          # ещё север
    assert compass_name(23) == "shimoli-sharq"   # уже северо-восток
    assert compass_name(90) == "sharq"
    assert compass_name(180) == "janub"
    assert compass_name(270) == "g'arb"
    assert compass_name(270, "ru") == "запад"


def test_inlet_edge_is_found_by_vertex_indices():
    """Сторона входа хранится индексами вершин: на вытянутом поле «с
    севера» может означать два разных ребра, и румба мало."""
    from suv.field_shape import edges, inlet_edge
    rect = rectangle(400, 200)
    first = edges(rect)[0]
    found = inlet_edge(rect, (first.i, first.j))
    assert found is not None and found.name("uz") == first.name("uz")
    assert inlet_edge(rect, None) is None
    assert inlet_edge(rect, (99, 100)) is None   # мусор не роняет расчёт


# ---------------------------------------------------------------- geojson

def test_geojson_ring_is_lon_lat_and_closed():
    """Copernicus ждёт [lon, lat]; перепутанные оси дают снимок другого
    полушария, и заметить это по числу гектаров невозможно."""
    ring = to_geojson_ring(rectangle(200, 200))
    assert ring[0] == ring[-1]
    lon, lat = ring[0]
    assert 66 < lon < 68 and 39 < lat < 40


def test_geojson_round_trip_keeps_the_area():
    pts = rectangle(350, 220)
    back = from_geojson_ring(to_geojson_ring(pts))
    assert len(back) == len(pts)
    assert area_ha(back) == pytest.approx(area_ha(pts), rel=1e-9)
