"""
Пакетный импорт границ полей: KML / KMZ / GeoJSON / Shapefile-zip.

Фаза 1 карты OneSoil, шаг «поле как объект»: у гиганта на пробе 100 га —
это десятки полей, и заводить их мастером бота по одному нельзя. Файл
границ у любого хозяйства с агрономом уже есть (GIS, кадастр, чужие
платформы умеют экспорт) — принимаем его и раскладываем в базу.

Правила модуля:
* Только WGS84 (широта/долгота). Shapefile с проекцией не в WGS84
  отклоняется с прямой подсказкой перепроецировать — молча пересчитать
  «на глаз» значит тихо сдвинуть чьё-то поле.
* Контур из GIS не режется под MAX_VERTICES обвода: у нарисованной в
  QGIS границы законно сотни вершин, Copernicus их переваривает. Поэтому
  здесь СВОЯ лёгкая проверка (замкнутость, площадь, оси), а не
  field_shape.validate, рассчитанный на обход углов ногами.
* Перепутанные оси ловятся: файл, где «широта» 55–74 и «долгота» 37–46,
  почти наверняка писан как lat,lon вместо lon,lat — об этом говорим,
  а не рисуем поле в Карском море.
* Один вызов seed() на файл — и бот, и CLI сеют одним путём.
"""

from __future__ import annotations

import io
import json
import math
import zipfile
from dataclasses import dataclass
from xml.etree import ElementTree

from .field_shape import area_ha as ring_area_ha
from .field_shape import from_geojson_ring

AREA_MIN_HA = 0.05
AREA_MAX_HA = 2000.0
MAX_FIELDS_PER_FILE = 200
NAME_KEYS = ("name", "nom", "nomi", "title", "field", "field_name", "id", "fid")


@dataclass
class ImportedField:
    name: str
    ring: list[list[float]]      # замкнутое кольцо [[lon, lat], ...]
    area_ha: float
    lat: float                   # центроид — точка поля для погоды/высоты
    lon: float


def parse_file(data: bytes, filename: str
               ) -> tuple[list[ImportedField], list[str]]:
    """Разобрать файл границ. Возвращает (поля, проблемы-строки).

    Проблемы не роняют разбор: один кривой контур в файле на тридцать
    полей — не повод отбрасывать двадцать девять целых.
    """
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    try:
        if ext in ("geojson", "json"):
            raw = _rings_from_geojson(data.decode("utf-8-sig"))
        elif ext == "kml":
            raw = _rings_from_kml(data)
        elif ext == "kmz":
            raw = _rings_from_kmz(data)
        elif ext == "zip":
            raw = _rings_from_zip(data)
        else:
            return [], [f"Формат .{ext} не поддерживается. "
                        "Нужен KML, KMZ, GeoJSON или ZIP с shapefile."]
    except Exception as exc:  # noqa: BLE001 — кривой файл, не наш баг
        return [], [f"Файл не читается: {type(exc).__name__}: {exc}"]

    fields: list[ImportedField] = []
    problems: list[str] = []
    for name, ring in raw[:MAX_FIELDS_PER_FILE]:
        got = _finish(name, ring)
        if isinstance(got, str):
            problems.append(got)
        else:
            fields.append(got)
    if len(raw) > MAX_FIELDS_PER_FILE:
        problems.append(f"В файле больше {MAX_FIELDS_PER_FILE} контуров — "
                        f"взяты первые {MAX_FIELDS_PER_FILE}.")
    if not fields and not problems:
        problems.append("В файле не нашлось ни одного полигона.")
    return fields, problems


# ------------------------------------------------------------- форматы

def _rings_from_geojson(text: str) -> list[tuple[str, list]]:
    doc = json.loads(text)
    feats = (doc.get("features", []) if doc.get("type") == "FeatureCollection"
             else [doc] if doc.get("type") == "Feature"
             else [{"geometry": doc, "properties": {}}])
    out = []
    for i, f in enumerate(feats, 1):
        geom = f.get("geometry") or {}
        name = _name_from_props(f.get("properties") or {}, i)
        for j, ring in enumerate(_polygon_rings(geom), 1):
            out.append((name if j == 1 else f"{name} #{j}", ring))
    return out


def _polygon_rings(geom: dict) -> list[list]:
    """Внешние кольца Polygon/MultiPolygon. Дырки (внутренние кольца)
    сознательно отбрасываются: полив считается по внешней границе."""
    t = geom.get("type")
    if t == "Polygon":
        return [geom["coordinates"][0]]
    if t == "MultiPolygon":
        return [poly[0] for poly in geom["coordinates"]]
    return []


def _name_from_props(props: dict, i: int) -> str:
    for k in props:
        if str(k).strip().lower() in NAME_KEYS and props[k] not in (None, ""):
            return str(props[k])[:40]
    return f"Dala {i}"


def _rings_from_kml(data: bytes) -> list[tuple[str, list]]:
    root = ElementTree.fromstring(data)
    out = []
    n = 0
    for pm in root.iter():
        if not pm.tag.endswith("Placemark"):
            continue
        n += 1
        name = f"Dala {n}"
        for child in pm.iter():
            if child.tag.endswith("name") and (child.text or "").strip():
                name = child.text.strip()[:40]
                break
        rings = []
        for poly in pm.iter():
            if not poly.tag.endswith("Polygon"):
                continue
            outer = None
            for el in poly.iter():
                if el.tag.endswith("outerBoundaryIs"):
                    outer = el
                    break
            src = outer if outer is not None else poly
            for coords in src.iter():
                if coords.tag.endswith("coordinates") and coords.text:
                    ring = []
                    for tok in coords.text.split():
                        parts = tok.split(",")
                        if len(parts) >= 2:
                            ring.append([float(parts[0]), float(parts[1])])
                    if ring:
                        rings.append(ring)
                    break
        for j, ring in enumerate(rings, 1):
            out.append((name if j == 1 else f"{name} #{j}", ring))
    return out


def _rings_from_kmz(data: bytes) -> list[tuple[str, list]]:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        kmls = [n for n in z.namelist() if n.lower().endswith(".kml")]
        if not kmls:
            raise ValueError("в KMZ нет KML")
        return _rings_from_kml(z.read(kmls[0]))


def _rings_from_zip(data: bytes) -> list[tuple[str, list]]:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        shp = [n for n in names if n.lower().endswith(".shp")]
        if shp:
            return _rings_from_shp_zip(z, shp[0])
        kml = [n for n in names if n.lower().endswith(".kml")]
        if kml:
            return _rings_from_kml(z.read(kml[0]))
        gj = [n for n in names if n.lower().endswith((".geojson", ".json"))]
        if gj:
            return _rings_from_geojson(z.read(gj[0]).decode("utf-8-sig"))
    raise ValueError("в ZIP нет ни shapefile, ни KML, ни GeoJSON")


def _rings_from_shp_zip(z: zipfile.ZipFile, shp_name: str
                        ) -> list[tuple[str, list]]:
    try:
        import shapefile  # pyshp
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("для shapefile нужен пакет pyshp "
                           "(pip install pyshp)") from exc

    base = shp_name[:-4]
    def member(ext):
        for n in z.namelist():
            if n.lower() == (base + ext).lower():
                return io.BytesIO(z.read(n))
        return None

    prj = member(".prj")
    if prj is not None:
        wkt = prj.read().decode("utf-8", "ignore")
        if "4326" not in wkt and "WGS" not in wkt.upper():
            raise ValueError("shapefile не в WGS84 — перепроецируйте в "
                             "EPSG:4326 (QGIS: Export → Save As → CRS)")

    reader = shapefile.Reader(shp=member(".shp"), dbf=member(".dbf"),
                              shx=member(".shx"))
    name_field = None
    for i, f in enumerate(reader.fields[1:]):      # [0] — DeletionFlag
        if str(f[0]).strip().lower() in NAME_KEYS:
            name_field = i
            break

    out = []
    for i, sr in enumerate(reader.shapeRecords(), 1):
        name = (str(sr.record[name_field])[:40]
                if name_field is not None and sr.record[name_field]
                else f"Dala {i}")
        geom = sr.shape.__geo_interface__
        for j, ring in enumerate(_polygon_rings(geom), 1):
            out.append((name if j == 1 else f"{name} #{j}",
                        [list(pt) for pt in ring]))
    return out


# ------------------------------------------------------------ проверка

def _finish(name: str, ring: list) -> ImportedField | str:
    if not ring or len(ring) < 3:
        return f"«{name}»: меньше трёх вершин — это не контур."
    ring = [[float(p[0]), float(p[1])] for p in ring]
    if ring[0] != ring[-1]:
        ring.append(list(ring[0]))

    lats = [p[1] for p in ring]
    lons = [p[0] for p in ring]
    if max(abs(v) for v in lats) > 90 or max(abs(v) for v in lons) > 180:
        return f"«{name}»: координаты вне диапазона широт/долгот."
    # Перепутанные оси: «широта» в диапазоне долгот Узбекистана и наоборот.
    if all(55 <= la <= 74 for la in lats) and all(37 <= lo <= 46 for lo in lons):
        return (f"«{name}»: похоже, оси перепутаны (файл писан lat,lon). "
                "Поменяйте порядок координат и загрузите снова.")

    pts = from_geojson_ring(ring)
    area = ring_area_ha(pts)
    if not AREA_MIN_HA <= area <= AREA_MAX_HA:
        return (f"«{name}»: площадь {area:.2f} га вне диапазона "
                f"{AREA_MIN_HA}–{AREA_MAX_HA} га.")
    lat_c = sum(lats[:-1]) / (len(lats) - 1)
    lon_c = sum(lons[:-1]) / (len(lons) - 1)
    return ImportedField(name=name, ring=ring, area_ha=round(area, 2),
                         lat=round(lat_c, 6), lon=round(lon_c, 6))


# --------------------------------------------------------------- посев

def seed(ledger, fields: list[ImportedField], *, owner_chat: int | None,
         crop_key: str, soil_key: str, irrigation_method: str,
         planting_iso: str, make_id, water_table_m: float = 0.0,
         elevation_for=None) -> list[str]:
    """Разложить разобранные поля в базу. Один путь для CLI и бота.

    make_id() зовётся на каждое поле — боту это даёт TG-{chat}-N,
    скрипту префиксные id. elevation_for(lat, lon) — необязательный
    добытчик высоты; None или сбой = 500 м, как в мастере.
    """
    created: list[str] = []
    for f in fields:
        fid = make_id()
        elev = None
        if elevation_for is not None:
            try:
                elev = elevation_for(f.lat, f.lon)
            except Exception:  # noqa: BLE001 — высота не роняет посев
                elev = None
        ledger.upsert_field(
            field_id=fid, name=f.name, owner_chat_id=owner_chat,
            hectares=f.area_ha, lat=f.lat, lon=f.lon,
            elevation_m=500.0 if elev is None else float(elev),
            crop_key=crop_key, soil_key=soil_key,
            planting_date=planting_iso, irrigation_method=irrigation_method,
            water_table_depth_m=water_table_m, baseline_m3_per_ha=None,
            baseline_interval_days=30, last_irrigation_date=None)
        # Контур — штатным save_polygon: он же выставляет area_ha и
        # сбрасывает то, что к новой границе не относится.
        ledger.save_polygon(fid, f.ring, f.area_ha, "import")
        created.append(fid)
    return created
