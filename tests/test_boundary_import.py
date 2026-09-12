"""
Пакетный импорт границ: KML/KMZ/GeoJSON/shapefile-zip -> поля в базе.

Правила, которые заперты тестами: только WGS84, перепутанные оси
ловятся словами, а не полем в Карском море; площадь вырезов вычитается,
а не остаётся в поле; самопересечённый контур отклоняется; из архива
берётся один файл, но об этом говорится вслух; один путь посева для CLI
и бота (boundary_import.seed).
"""

import io
import json
import zipfile
from datetime import date

import pytest

from suv.boundary_import import (AREA_MIN_HA, AREA_NOT_CROSSCHECKED,
                                 ImportedField, parse_file, seed)
from suv.ledger import Ledger

# Квадрат ~2,2 га под Самаркандом (150x150 м примерно).
SQ = [[66.996, 39.558], [66.9977, 39.558], [66.9977, 39.5593],
      [66.996, 39.5593], [66.996, 39.558]]
SQ2 = [[67.010, 39.560], [67.0117, 39.560], [67.0117, 39.5613],
       [67.010, 39.5613], [67.010, 39.560]]


def _gj(features):
    return json.dumps({"type": "FeatureCollection", "features": features}
                      ).encode()


def _feature(ring, name=None, holes=None):
    coords = [ring] + (holes or [])
    props = {"Name": name} if name else {}
    return {"type": "Feature", "properties": props,
            "geometry": {"type": "Polygon", "coordinates": coords}}


# ---------------------------------------------------------------- geojson

def test_geojson_two_fields_with_names_and_areas():
    data = _gj([_feature(SQ, "Shimoliy"), _feature(SQ2, "Janubiy")])
    fields, problems = parse_file(data, "granicy.geojson")
    # Единственная оговорка на чистом файле — про несверенную площадь.
    assert problems == [AREA_NOT_CROSSCHECKED]
    assert [f.name for f in fields] == ["Shimoliy", "Janubiy"]
    f = fields[0]
    assert 1.5 < f.area_ha < 3.5
    assert f.ring[0] == f.ring[-1]                 # кольцо замкнуто
    assert 39.558 < f.lat < 39.560 and 66.996 < f.lon < 66.998


def test_geojson_hole_area_is_subtracted_and_open_ring_closed():
    ring_open = SQ[:-1]                            # не замкнуто
    hole = [[66.9965, 39.5585], [66.9968, 39.5585], [66.9968, 39.5588],
            [66.9965, 39.5585]]
    data = _gj([_feature(ring_open, "L", holes=[hole])])
    fields, _ = parse_file(data, "x.json")
    assert len(fields) == 1
    assert fields[0].ring[0] == fields[0].ring[-1]
    # Вырез вычтен: поле меньше сплошного квадрата на площадь выреза.
    solid, _ = parse_file(_gj([_feature(SQ, "L")]), "x.json")
    assert fields[0].area_ha < solid[0].area_ha


def test_multipolygon_becomes_numbered_fields():
    geom = {"type": "MultiPolygon", "coordinates": [[SQ], [SQ2]]}
    data = json.dumps({"type": "Feature", "properties": {"name": "Klin"},
                       "geometry": geom}).encode()
    fields, _ = parse_file(data, "x.geojson")
    assert [f.name for f in fields] == ["Klin", "Klin #2"]


def test_swapped_axes_are_called_out_not_planted_in_the_sea():
    swapped = [[p[1], p[0]] for p in SQ]           # lat,lon вместо lon,lat
    data = _gj([_feature(swapped, "Adashgan")])
    fields, problems = parse_file(data, "x.geojson")
    assert fields == []
    # Отказ читает фермер в узбекском чате, а не разработчик в логе.
    assert any("o'qlar almashib" in p for p in problems)


def test_tiny_polygon_is_rejected_with_its_area():
    tiny = [[66.996, 39.558], [66.99601, 39.558], [66.99601, 39.55801],
            [66.996, 39.558]]
    _, problems = parse_file(_gj([_feature(tiny, "Nuqta")]), "x.geojson")
    assert any("oralig'idan tashqarida" in p for p in problems)
    assert AREA_MIN_HA == 0.05


def test_one_bad_ring_does_not_kill_the_rest():
    data = _gj([_feature(SQ, "OK"), _feature(SQ[:2], "Broken")])
    fields, problems = parse_file(data, "x.geojson")
    assert [f.name for f in fields] == ["OK"]
    assert len(problems) == 2                      # кривой контур + оговорка


# ------------------------------------------------------------------- kml

KML = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document>
<Placemark><name>Olma bogʻi</name><Polygon><outerBoundaryIs><LinearRing>
<coordinates>
66.996,39.558,0 66.9977,39.558,0 66.9977,39.5593,0 66.996,39.5593,0 66.996,39.558,0
</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>
<Placemark><Polygon><outerBoundaryIs><LinearRing>
<coordinates>67.010,39.560 67.0117,39.560 67.0117,39.5613 67.010,39.5613</coordinates>
</LinearRing></outerBoundaryIs></Polygon></Placemark>
</Document></kml>"""


def test_kml_and_kmz():
    fields, problems = parse_file(KML.encode(), "granicy.kml")
    assert problems == [AREA_NOT_CROSSCHECKED]
    assert fields[0].name.startswith("Olma")
    assert fields[1].name == "Dala 2"              # безымянный получает номер

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("doc.kml", KML)
    fields2, _ = parse_file(buf.getvalue(), "granicy.kmz")
    assert len(fields2) == 2
    assert fields2[0].area_ha == fields[0].area_ha


# -------------------------------------------------------------- shapefile

def _shp_zip(prj_wkt=None):
    import shapefile
    shp, dbf, shx = io.BytesIO(), io.BytesIO(), io.BytesIO()
    w = shapefile.Writer(shp=shp, dbf=dbf, shx=shx)
    w.field("NAME", "C")
    w.poly([SQ])
    w.record("Gigant-1")
    w.close()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("fields.shp", shp.getvalue())
        z.writestr("fields.dbf", dbf.getvalue())
        z.writestr("fields.shx", shx.getvalue())
        if prj_wkt:
            z.writestr("fields.prj", prj_wkt)
    return buf.getvalue()


def test_shapefile_zip_with_wgs84():
    wgs = ('GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",'
           'SPHEROID["WGS_1984",6378137,298.257223563]]]')
    fields, problems = parse_file(_shp_zip(wgs), "fields.zip")
    assert problems == [AREA_NOT_CROSSCHECKED]
    assert fields[0].name == "Gigant-1"
    assert 1.5 < fields[0].area_ha < 3.5


def test_shapefile_in_utm_is_refused_with_advice():
    utm = 'PROJCS["WGS 84 / UTM zone 42N"...]'    # PROJCS, но есть WGS…
    # Настоящий отказ — на проекции без WGS/4326 в имени:
    utm = 'PROJCS["Pulkovo_1942_GK_Zone_12",AUTHORITY["EPSG","28412"]]'
    fields, problems = parse_file(_shp_zip(utm), "fields.zip")
    assert fields == []
    assert any("WGS84" in p for p in problems)


# ----------------------------------------------------------------- посев

def test_seed_writes_polygon_area_and_owner(tmp_path):
    led = Ledger(tmp_path / "t.db")
    fields, _ = parse_file(
        _gj([_feature(SQ, "Shimoliy"), _feature(SQ2, "Janubiy")]),
        "x.geojson")
    n = [0]

    def make_id():
        n[0] += 1
        return f"GIG-{n[0]:03d}"

    created = seed(led, fields, owner_chat=555, crop_key="winter_wheat",
                   soil_key="loam", irrigation_method="furrow",
                   planting_iso="2026-10-05", make_id=make_id,
                   elevation_for=lambda lat, lon: 655.0)
    assert created == ["GIG-001", "GIG-002"]
    import sqlite3
    c = sqlite3.connect(led.path)
    c.row_factory = sqlite3.Row
    row = c.execute("SELECT * FROM fields WHERE field_id='GIG-001'").fetchone()
    assert row["name"] == "Shimoliy" and row["owner_chat_id"] == 555
    assert row["crop_key"] == "winter_wheat"
    assert row["elevation_m"] == pytest.approx(655.0)
    assert row["polygon_source"] == "import"
    assert row["area_ha"] == pytest.approx(fields[0].area_ha, abs=0.05)
    assert row["hectares"] == pytest.approx(fields[0].area_ha, abs=0.05)
    ring = json.loads(row["polygon_geojson"])
    assert ring[0] == ring[-1] and len(ring) >= 5


def test_seed_survives_elevation_failure(tmp_path):
    led = Ledger(tmp_path / "t.db")
    fields, _ = parse_file(_gj([_feature(SQ, "X")]), "x.geojson")

    def boom(lat, lon):
        raise RuntimeError("network down")

    created = seed(led, fields, owner_chat=None, crop_key="cotton",
                   soil_key="loam", irrigation_method="furrow",
                   planting_iso="2026-04-10", make_id=lambda: "IMP-001",
                   elevation_for=boom)
    assert created == ["IMP-001"]
    import sqlite3
    with sqlite3.connect(led.path) as c:
        elev = c.execute("SELECT elevation_m FROM fields").fetchone()[0]
    assert elev == 500.0


def test_import_date_rules_match_wizard():
    """Планка даты как в мастере: месяц -> этот год либо прошлый."""
    from datetime import date as d
    today = d(2026, 9, 11)
    month = 10                                     # октябрь ещё не наступил
    year = today.year if month <= today.month else today.year - 1
    assert year == 2025


# ------------------------------------------------- геометрия импорта
#
# Четыре тихие потери 12.09.2026: вырез оставался в площади, «бабочка»
# проходила как поле, из архива молча брался первый файл, а точка поля
# (та, по которой берётся ПОГОДА) считалась средним вершин и на вогнутом
# контуре оказывалась вне поля.

def _pt(east_m, north_m, lat0=39.558, lon0=66.996):
    """Точка в метрах от угла под Самаркандом -> [lon, lat]."""
    from suv.field_shape import meters_per_degree
    m_lat, m_lon = meters_per_degree(lat0)
    return [round(lon0 + east_m / m_lon, 9), round(lat0 + north_m / m_lat, 9)]


def _inside(ring, lon, lat):
    """Луч на восток: нечётное число пересечений — точка внутри."""
    ins = False
    for i in range(len(ring) - 1):
        x1, y1 = ring[i]
        x2, y2 = ring[i + 1]
        if (y1 > lat) != (y2 > lat):
            if lon < x1 + (lat - y1) * (x2 - x1) / (y2 - y1):
                ins = not ins
    return ins


def test_hole_area_leaves_the_field_not_only_the_drawing():
    """Вырез 34,34 га внутри контура 95,35 га — это поле 61,01 га.

    До правки импортировалось 95,35 га: плюс 56% гектаров, а норма
    считается мм x 10 x га — значит, плюс 56% воды в каждой
    рекомендации по этому полю. Заметить было нечем: и заявленная, и
    обмеренная площадь приходили из одного файла.
    """
    outer = [_pt(0, 0), _pt(976, 0), _pt(976, 977), _pt(0, 977), _pt(0, 0)]
    hole = [_pt(195, 195), _pt(781, 195), _pt(781, 781), _pt(195, 781),
            _pt(195, 195)]
    fields, _ = parse_file(_gj([_feature(outer, "Kesikli", holes=[hole])]),
                           "x.geojson")
    assert fields[0].area_ha == pytest.approx(61.01, abs=0.1)


def test_selfcrossing_ring_is_refused_not_measured():
    """«Бабочка» — не поле, и площадь по ней не площадь.

    Несимметричная петля проходила как валидное поле 0,45 га: формула
    шнурков на самопересечении даёт разность двух петель, и эта разность
    выглядит правдоподобным числом. Проверка is_simple была написана для
    обвода ногами — на импорте её просто не звали.
    """
    bow = [_pt(0, 0), _pt(200, 150), _pt(0, 150), _pt(140, 0), _pt(0, 0)]
    fields, problems = parse_file(_gj([_feature(bow, "Kapalak")]),
                                  "x.geojson")
    assert fields == []
    # Отказ читает фермер в узбекском чате, а не разработчик в логе.
    assert any("o'zini kesib o'tadi" in p for p in problems)


def test_second_kml_in_archive_is_named_not_swallowed():
    """Из архива берём первый файл — но говорим, что взяли не всё."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("brigada-1.kml", KML)
        z.writestr("brigada-2.kml", KML)
    fields, problems = parse_file(buf.getvalue(), "hammasi.kmz")
    assert len(fields) == 2                        # из первого файла
    assert any("brigada-2.kml" in p for p in problems)


def test_field_point_lands_inside_a_concave_field():
    """Точка поля — это точка, по которой берётся погода.

    Поле подковой вокруг усадьбы: среднее вершин попадало в вырез между
    её концами, то есть прогноз брался за границей поля. Ни одна цифра
    в карточке этого не показывала. Одного центроида площади мало — у
    подковы и он лежит в вырезе, это проверено отдельно.
    """
    horse = [_pt(0, 0), _pt(600, 0), _pt(600, 600), _pt(450, 600),
             _pt(450, 150), _pt(150, 150), _pt(150, 600), _pt(0, 600),
             _pt(0, 0)]
    fields, _ = parse_file(_gj([_feature(horse, "Taqa")]), "x.geojson")
    f = fields[0]
    assert _inside(horse, f.lon, f.lat), "точка поля оказалась вне поля"


def test_field_point_does_not_land_in_the_hole():
    """У поля-бублика точка не имеет права попасть в вырез."""
    outer = [_pt(0, 0), _pt(900, 0), _pt(900, 900), _pt(0, 900), _pt(0, 0)]
    hole = [_pt(150, 150), _pt(750, 150), _pt(750, 750), _pt(150, 750),
            _pt(150, 150)]
    fields, _ = parse_file(_gj([_feature(outer, "Halqa", holes=[hole])]),
                           "x.geojson")
    f = fields[0]
    assert _inside(outer, f.lon, f.lat) and not _inside(hole, f.lon, f.lat)


def test_repeated_vertex_is_noise_not_a_self_crossing():
    """Отказ по самопересечению не имеет права съедать честные файлы.

    В кадастровом экспорте точка нередко записана дважды подряд, а
    кольцо — замкнуто двумя одинаковыми вершинами. Для is_simple,
    писанной под обвод ногами, это дубль вершины и повод отказать всему
    полю. Для импорта — мусор, который ничего не меняет ни в площади,
    ни в форме.
    """
    twice_closed = SQ + [SQ[0]]                    # две одинаковые вершины в конце
    mid_dup = [SQ[0], SQ[1], SQ[1], SQ[2], SQ[3], SQ[0]]
    plain, _ = parse_file(_gj([_feature(SQ, "Обычное")]), "x.geojson")
    for ring, why in ((twice_closed, "двойное замыкание"),
                      (mid_dup, "повтор вершины внутри")):
        fields, problems = parse_file(_gj([_feature(ring, "X")]), "x.geojson")
        assert len(fields) == 1, f"{why}: поле отклонено — {problems}"
        assert fields[0].area_ha == plain[0].area_ha


# ---------------------------- регрессии проверки на самопересечение
#
# Правка 12.09.2026 подключила is_simple к импорту — и в первом виде
# отказала честным файлам и стоила секунд. Оба замка ниже про это.

def test_cadastral_duplicate_vertex_is_collapsed_not_refused():
    """Вершина, записанная дважды со сдвигом в девятом знаке.

    Для float это разные числа, а has_duplicate_points округляет до
    миллиметра и считает их дублем: кадастровый экспорт получал отказ
    «контур пересекает сам себя» на ровном месте. Схлопываем по
    РАССТОЯНИЮ, а не по точному равенству.
    """
    ring = [[67.0, 39.5], [67.008, 39.5], [67.008, 39.500000001],
            [67.008, 39.506], [67.0, 39.506], [67.0, 39.5]]
    fields, problems = parse_file(_gj([_feature(ring, "Kadastr")]), "k.geojson")
    assert len(fields) == 1, problems
    assert fields[0].area_ha > 0


def test_a_real_butterfly_is_still_refused():
    """Обратная сторона того же замка: настоящее самопересечение."""
    bow = [[67.0, 39.5], [67.004, 39.504], [67.0, 39.504],
           [67.006, 39.5], [67.0, 39.5]]
    fields, problems = parse_file(_gj([_feature(bow, "Kapalak")]), "b.geojson")
    assert fields == []
    assert any("o'zini kesib o'tadi" in p for p in problems)


def test_simplicity_budget_is_counted_per_file_not_per_ring():
    """Потолок на ОДИН контур ничего не обещает про файл.

    200 полей по 400 вершин — это 32 млн пар рёбер и одиннадцать секунд
    под «typing…». Бюджет общий на разбор; кончился — оставшиеся контуры
    проверку пропускают и говорят об этом, а не молчат.
    """
    import json
    import math

    from suv.boundary_import import SIMPLE_CHECK_BUDGET_PAIRS, parse_file

    def circle(cx, n=400, rad=0.002):
        pts = [(cx + rad * math.cos(2 * math.pi * i / n),
                39.5 + rad * math.sin(2 * math.pi * i / n)) for i in range(n)]
        return pts + [pts[0]]

    feats = [{"type": "Feature", "properties": {"name": f"P{i}"},
              "geometry": {"type": "Polygon",
                           "coordinates": [[list(p) for p in circle(67.0 + 0.01 * i)]]}}
             for i in range(30)]
    data = json.dumps({"type": "FeatureCollection", "features": feats}).encode()
    fields, notes = parse_file(data, "big.geojson")

    assert len(fields) == 30, "поля потерялись из-за бюджета проверки"
    skipped = [n for n in notes if "tekshirilmadi" in n]
    assert skipped, "бюджет не кончился — проверка не защищена от больших файлов"
    assert SIMPLE_CHECK_BUDGET_PAIRS // (400 * 400) < 30


def test_a_hole_outside_the_field_is_not_subtracted():
    """Вычитать можно только вырез, лежащий ВНУТРИ своего поля.

    Кольцо рядом или поверх внешнего — мусор экспорта либо чужая
    геометрия; вычесть его значит отнять у поля гектары, которых никто
    не вырезал. В прогоне поле худело с 95 до 80 га, а вырез больше
    кольца уводил площадь в минус.
    """
    import json

    from suv.boundary_import import parse_file

    outer = [[67.0, 39.5], [67.011, 39.5], [67.011, 39.508],
             [67.0, 39.508], [67.0, 39.5]]
    far = [[67.05, 39.5], [67.055, 39.5], [67.055, 39.504],
           [67.05, 39.504], [67.05, 39.5]]
    gj = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"name": "Chetdagi teshik"},
         "geometry": {"type": "Polygon", "coordinates": [outer, far]}}]}
    fields, notes = parse_file(json.dumps(gj).encode(), "x.geojson")

    assert len(fields) == 1
    assert fields[0].area_ha == pytest.approx(84.03, abs=0.05)
    assert any("dala ichida emas" in n for n in notes)
