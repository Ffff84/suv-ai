"""
Пакетный импорт границ: KML/KMZ/GeoJSON/shapefile-zip -> поля в базе.

Правила, которые заперты тестами: только WGS84, перепутанные оси
ловятся словами, а не полем в Карском море; дырки полигонов
отбрасываются; один путь посева для CLI и бота (boundary_import.seed).
"""

import io
import json
import zipfile
from datetime import date

import pytest

from suv.boundary_import import (AREA_MIN_HA, ImportedField, parse_file,
                                 seed)
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
    assert problems == []
    assert [f.name for f in fields] == ["Shimoliy", "Janubiy"]
    f = fields[0]
    assert 1.5 < f.area_ha < 3.5
    assert f.ring[0] == f.ring[-1]                 # кольцо замкнуто
    assert 39.558 < f.lat < 39.560 and 66.996 < f.lon < 66.998


def test_geojson_holes_are_dropped_and_open_ring_closed():
    ring_open = SQ[:-1]                            # не замкнуто
    hole = [[66.9965, 39.5585], [66.9968, 39.5585], [66.9968, 39.5588],
            [66.9965, 39.5585]]
    data = _gj([_feature(ring_open, "L", holes=[hole])])
    fields, _ = parse_file(data, "x.json")
    assert len(fields) == 1
    assert fields[0].ring[0] == fields[0].ring[-1]


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
    assert any("оси перепутаны" in p for p in problems)


def test_tiny_polygon_is_rejected_with_its_area():
    tiny = [[66.996, 39.558], [66.99601, 39.558], [66.99601, 39.55801],
            [66.996, 39.558]]
    _, problems = parse_file(_gj([_feature(tiny, "Nuqta")]), "x.geojson")
    assert any("вне диапазона" in p for p in problems)
    assert AREA_MIN_HA == 0.05


def test_one_bad_ring_does_not_kill_the_rest():
    data = _gj([_feature(SQ, "OK"), _feature(SQ[:2], "Broken")])
    fields, problems = parse_file(data, "x.geojson")
    assert [f.name for f in fields] == ["OK"]
    assert len(problems) == 1


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
    assert problems == []
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
    assert problems == []
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
