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
from .field_shape import is_simple
from .field_shape import to_local_m

AREA_MIN_HA = 0.05
AREA_MAX_HA = 2000.0
MAX_FIELDS_PER_FILE = 200
NAME_KEYS = ("name", "nom", "nomi", "title", "field", "field_name", "id", "fid")

# Выше этого числа вершин полный перебор пар рёбер (is_simple, O(n^2))
# стоит четверть секунды на поле, а полей в файле бывает две сотни.
# Такой контур на самопересечение не проверяем — и говорим об этом.
SIMPLE_CHECK_MAX_VERTICES = 500
# Перебор пар рёбер квадратичен, и потолок на ОДИН контур ничего не
# обещает про файл: 200 полей по 400 вершин — это 32 млн пар и 11
# секунд под «typing…». Считаем бюджет на весь разбор; кончился —
# оставшиеся контуры проверку пропускают и говорят об этом.
SIMPLE_CHECK_BUDGET_PAIRS = 2_000_000
# Ближе этого вершины считаем одной точкой: миллиметр — тот же порог,
# которым has_duplicate_points ловит дубль при обводе ногами.
SAME_POINT_M = 0.001

# У поля из мастера две НЕЗАВИСИМЫЕ цифры площади: hectares со слов
# фермера и area_ha по обводке; их спор ловит AREA_MISMATCH_FRAC на 10%.
# У импортированного цифра ОДНА — из файла, — и сверка молчит всегда.
# Молчание проверки, которой не было, читается как «сверено»; говорим
# вслух, потому что объём полива считается именно по hectares.
AREA_NOT_CROSSCHECKED = (
    "Maydon fayldan olindi — chegara ham o'sha fayldan. Solishtirishga "
    "ikkinchi raqam yo'q, 10% tekshiruvi bo'lmadi. Gektarni hujjat "
    "bo'yicha o'zingiz tekshiring.")


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
    # Предупреждения про сам файл (а не про контуры в нём) копятся
    # отдельно: их место — в начале списка, до разбора колец.
    notes: list[str] = []
    try:
        if ext in ("geojson", "json"):
            raw = _rings_from_geojson(data.decode("utf-8-sig"))
        elif ext == "kml":
            raw = _rings_from_kml(data)
        elif ext == "kmz":
            raw = _rings_from_kmz(data, notes)
        elif ext == "zip":
            raw = _rings_from_zip(data, notes)
        else:
            return [], [f".{ext} formatini o'qiy olmayman. "
                        "KML, KMZ, GeoJSON yoki ZIP (shapefile) kerak."]
    except Exception as exc:  # noqa: BLE001 — кривой файл, не наш баг
        return [], [f"Fayl o'qilmadi: {type(exc).__name__}: {exc}"]

    fields: list[ImportedField] = []
    problems: list[str] = []
    # Бюджет проверки на самопересечение — общий на файл, список из
    # одного числа вместо nonlocal: _finish его уменьшает по месту.
    budget = [SIMPLE_CHECK_BUDGET_PAIRS]
    for name, ring, holes in raw[:MAX_FIELDS_PER_FILE]:
        got = _finish(name, ring, holes, problems, budget)
        if isinstance(got, str):
            problems.append(got)
        else:
            fields.append(got)
    if len(raw) > MAX_FIELDS_PER_FILE:
        problems.append(f"Faylda {MAX_FIELDS_PER_FILE} tadan ko'p chegara "
                        f"bor — birinchi {MAX_FIELDS_PER_FILE} tasi olindi.")
    if not fields and not problems:
        problems.append("Faylda birorta ham dala chegarasi topilmadi.")
    # Оговорка про площадь идёт в хвосте: она про весь файл и одинакова
    # всегда, а строки про конкретные поля фермеру нужнее.
    tail = [AREA_NOT_CROSSCHECKED] if fields else []
    return fields, notes + problems + tail


# ------------------------------------------------------------- форматы

def _rings_from_geojson(text: str) -> list[tuple[str, list, list]]:
    doc = json.loads(text)
    feats = (doc.get("features", []) if doc.get("type") == "FeatureCollection"
             else [doc] if doc.get("type") == "Feature"
             else [{"geometry": doc, "properties": {}}])
    out = []
    for i, f in enumerate(feats, 1):
        geom = f.get("geometry") or {}
        name = _name_from_props(f.get("properties") or {}, i)
        for j, (ring, holes) in enumerate(_polygon_rings(geom), 1):
            out.append((name if j == 1 else f"{name} #{j}", ring, holes))
    return out


def _polygon_rings(geom: dict) -> list[tuple[list, list]]:
    """Polygon/MultiPolygon как пары (внешнее кольцо, вырезы).

    «Полив считается по внешней границе» звучало разумно, пока вырез
    отбрасывали вместе с его площадью: контур 95,35 га с вырезом
    34,34 га заводился как 95,35 га — плюс 56% гектаров, а норма
    считается мм x 10 x га, значит и плюс 56% воды. Ни в усадьбу, ни в
    пруд, ни в чужой клин посреди поля воду не льют, поэтому вырезы
    едут дальше и вычитаются в _finish.
    """
    t = geom.get("type")
    if t == "Polygon":
        rings = geom["coordinates"]
        return [(rings[0], list(rings[1:]))]
    if t == "MultiPolygon":
        return [(poly[0], list(poly[1:])) for poly in geom["coordinates"]]
    return []


def _name_from_props(props: dict, i: int) -> str:
    for k in props:
        if str(k).strip().lower() in NAME_KEYS and props[k] not in (None, ""):
            return str(props[k])[:40]
    return f"Dala {i}"


def _rings_from_kml(data: bytes) -> list[tuple[str, list, list]]:
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
            inners = []
            for el in poly.iter():
                if el.tag.endswith("outerBoundaryIs") and outer is None:
                    outer = el
                elif el.tag.endswith("innerBoundaryIs"):
                    inners.append(el)
            src = outer if outer is not None else poly
            ring = _kml_ring(src)
            if not ring:
                continue
            # Вырезы берём только там, где внешнее кольцо названо явно:
            # без outerBoundaryIs неизвестно, какое из колец внешнее, и
            # вычесть не то значит занизить поле вместо того, чтобы его
            # завысить. Завышение мы чиним, занижение — заводим новое.
            holes = ([h for h in (_kml_ring(e) for e in inners) if h]
                     if outer is not None else [])
            rings.append((ring, holes))
        for j, (ring, holes) in enumerate(rings, 1):
            out.append((name if j == 1 else f"{name} #{j}", ring, holes))
    return out


def _kml_ring(el) -> list[list[float]]:
    """Первое <coordinates> внутри элемента как кольцо [[lon, lat], ...]."""
    for coords in el.iter():
        if coords.tag.endswith("coordinates") and coords.text:
            ring = []
            for tok in coords.text.split():
                parts = tok.split(",")
                if len(parts) >= 2:
                    ring.append([float(parts[0]), float(parts[1])])
            return ring
    return []


def _skipped(notes: list[str], kind: str, picked: str,
             rest: list[str]) -> None:
    """Сказать вслух, что из архива взят один файл, а лежало несколько.

    Молчать нельзя: в архиве от агронома лежит разбивка по бригадам или
    по годам, фермер грузит его целиком и получает четверть своих полей
    — без единого слова о том, куда делись остальные. Мы всё равно
    берём первый (склеивать чужие наборы вслепую хуже), но теперь это
    видно.
    """
    tail = ", ".join(rest[:3]) + ("…" if len(rest) > 3 else "")
    notes.append(f"Arxivda {len(rest) + 1} ta {kind} fayl bor — faqat "
                 f"«{picked}» olindi, qolgani o'tkazib yuborildi ({tail}). "
                 "Ularni alohida yuboring.")


def _rings_from_kmz(data: bytes, notes: list[str]) -> list[tuple]:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        kmls = [n for n in z.namelist() if n.lower().endswith(".kml")]
        if not kmls:
            raise ValueError("в KMZ нет KML")
        if len(kmls) > 1:
            _skipped(notes, ".kml", kmls[0], kmls[1:])
        return _rings_from_kml(z.read(kmls[0]))


def _rings_from_zip(data: bytes, notes: list[str]) -> list[tuple]:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        shp = [n for n in names if n.lower().endswith(".shp")]
        if shp:
            if len(shp) > 1:
                _skipped(notes, ".shp", shp[0], shp[1:])
            return _rings_from_shp_zip(z, shp[0])
        kml = [n for n in names if n.lower().endswith(".kml")]
        if kml:
            if len(kml) > 1:
                _skipped(notes, ".kml", kml[0], kml[1:])
            return _rings_from_kml(z.read(kml[0]))
        gj = [n for n in names if n.lower().endswith((".geojson", ".json"))]
        if gj:
            if len(gj) > 1:
                _skipped(notes, ".geojson", gj[0], gj[1:])
            return _rings_from_geojson(z.read(gj[0]).decode("utf-8-sig"))
    raise ValueError("в ZIP нет ни shapefile, ни KML, ни GeoJSON")


def _rings_from_shp_zip(z: zipfile.ZipFile, shp_name: str
                        ) -> list[tuple[str, list, list]]:
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
        for j, (ring, holes) in enumerate(_polygon_rings(geom), 1):
            out.append((name if j == 1 else f"{name} #{j}",
                        [list(pt) for pt in ring],
                        [[list(pt) for pt in h] for h in holes]))
    return out


# ------------------------------------------------------------ проверка

def _closed(ring: list[list[float]]) -> list[list[float]]:
    """Кольцо с повторённой первой вершиной в конце."""
    return ring if ring[0] == ring[-1] else ring + [list(ring[0])]


def _point_inside(rings: list[list[list[float]]], lon: float,
                  lat: float) -> bool:
    """Точка внутри поля: луч на восток, нечётное число пересечений.

    Кольца берутся все разом — внешнее вместе с вырезами: чётно-нечётное
    правило само выбрасывает точку, попавшую в вырез.
    """
    inside = False
    for ring in rings:
        for i in range(len(ring) - 1):
            x1, y1 = ring[i]
            x2, y2 = ring[i + 1]
            if (y1 > lat) != (y2 > lat):
                if lon < x1 + (lat - y1) * (x2 - x1) / (y2 - y1):
                    inside = not inside
    return inside


def _field_point(rings: list[list[list[float]]]) -> tuple[float, float]:
    """Точка поля (lat, lon) — по ней берутся погода, высота и снимок.

    Среднее вершин для этого не годится дважды. Оно тянется туда, где
    вершин гуще (а обводчик сажает их чаще вдоль кривого края), и на
    вогнутом контуре — поле подковой вокруг усадьбы, клин вдоль
    излучины — уезжает ВНЕ поля. Прогноз тогда берётся не для того
    места, и по цифрам этого не видно.

    Берём центр масс площади. У подковы и он лежит в вырезе, поэтому
    попадание проверяем, и при промахе опускаемся на горизонталь через
    центр масс: середина самого длинного её куска, лежащего в поле,
    внутри по построению.
    """
    outer = rings[0]
    a2 = cx = cy = 0.0
    for i in range(len(outer) - 1):
        x1, y1 = outer[i]
        x2, y2 = outer[i + 1]
        cr = x1 * y2 - x2 * y1
        a2 += cr
        cx += (x1 + x2) * cr
        cy += (y1 + y2) * cr
    lons = [p[0] for p in outer[:-1]]
    lats = [p[1] for p in outer[:-1]]
    if abs(a2) < 1e-18:            # вырожденное кольцо — спасаемся средним
        return sum(lats) / len(lats), sum(lons) / len(lons)
    # Проекция в метры — аффинная (растяжение по осям), а центр масс
    # аффинное преобразование переносит без поправок: считаем в градусах.
    lon_c, lat_c = cx / (3.0 * a2), cy / (3.0 * a2)
    if _point_inside(rings, lon_c, lat_c):
        return lat_c, lon_c
    xs = []
    for ring in rings:
        for i in range(len(ring) - 1):
            x1, y1 = ring[i]
            x2, y2 = ring[i + 1]
            if (y1 > lat_c) != (y2 > lat_c):
                xs.append(x1 + (lat_c - y1) * (x2 - x1) / (y2 - y1))
    xs.sort()
    best_span, best_lon = 0.0, None
    for i in range(0, len(xs) - 1, 2):
        if xs[i + 1] - xs[i] > best_span:
            best_span, best_lon = xs[i + 1] - xs[i], (xs[i] + xs[i + 1]) / 2.0
    if best_lon is None:           # горизонталь не пересекла поле
        return sum(lats) / len(lats), sum(lons) / len(lons)
    return lat_c, best_lon


def _collapse_repeats(ring: list) -> list:
    """Схлопнуть подряд идущие вершины, отстоящие меньше чем на миллиметр.

    Вершина, записанная подряд дважды, — не самопересечение, а мусор
    экспорта: в кадастровом shapefile точка часто лежит дважды, а кольцо
    бывает замкнуто двумя одинаковыми вершинами. Сравнение на ТОЧНОЕ
    равенство этого не ловило: сдвиг в девятом знаке после запятой — это
    десятые доли микрона на местности, но для float это разные числа, а
    has_duplicate_points округляет до миллиметра и считает их дублем.
    Итог — отказ «контур пересекает сам себя» честному файлу из GIS.
    """
    if len(ring) < 2:
        return list(ring)
    xy = to_local_m([(p[1], p[0]) for p in ring])
    out = [list(ring[0])]
    keep = [xy[0]]
    for i in range(1, len(ring)):
        x, y = xy[i]
        px, py = keep[-1]
        if abs(x - px) <= SAME_POINT_M and abs(y - py) <= SAME_POINT_M:
            continue
        out.append(list(ring[i]))
        keep.append((x, y))
    return out


def _finish(name: str, ring: list, holes: list | None = None,
            notes: list[str] | None = None,
            budget_left: list[int] | None = None) -> ImportedField | str:
    if not ring or len(ring) < 3:
        return f"«{name}»: uchtadan kam nuqta — bu chegara emas."
    ring = [[float(p[0]), float(p[1])] for p in ring]
    if ring[0] != ring[-1]:
        ring.append(list(ring[0]))
    # Вершина, записанная подряд дважды, — не самопересечение, а мусор
    # экспорта: в кадастровом shapefile точка часто лежит дважды, а
    # кольцо бывает замкнуто двумя одинаковыми вершинами. Схлопываем
    # их ДО проверки на простоту: has_duplicate_points писался под
    # обвод ногами («этот угол уже есть»), и без этого честный файл
    # из GIS получал отказ «контур пересекает сам себя» на ровном месте.
    ring = _collapse_repeats(ring)
    if len(ring) < 4:              # разных вершин осталось меньше трёх
        return f"«{name}»: uchtadan kam turli nuqta — bu chegara emas."

    lats = [p[1] for p in ring]
    lons = [p[0] for p in ring]
    if max(abs(v) for v in lats) > 90 or max(abs(v) for v in lons) > 180:
        return f"«{name}»: koordinatalar chegaradan tashqarida."
    # Перепутанные оси: «широта» в диапазоне долгот Узбекистана и наоборот.
    if all(55 <= la <= 74 for la in lats) and all(37 <= lo <= 46 for lo in lons):
        return (f"«{name}»: o'qlar almashib ketganga o'xshaydi (fayl "
                "lat,lon tartibida). Tartibni almashtirib, qayta yuboring.")

    pts = from_geojson_ring(ring)
    # Самопересечение из GIS приходит чаще, чем кажется: «бабочка» после
    # неудачной правки вершины проходила как валидное поле, а площадь по
    # формуле шнурков на ней — разность двух петель, а не площадь поля.
    # Проверка уже написана в field_shape и стоит там за обводом — здесь
    # она нужна ровно за тем же.
    n = len(pts)
    budget = n * n if budget_left is None else budget_left[0]
    if n <= SIMPLE_CHECK_MAX_VERTICES and n * n <= budget:
        if budget_left is not None:
            budget_left[0] -= n * n
        if not is_simple(pts):
            return (f"«{name}»: chegara o'zini kesib o'tadi — bunday "
                    "kontur bo'yicha maydon noto'g'ri chiqadi. Chegarani "
                    "GIS'da to'g'rilang (QGIS: Vector → Geometry Tools "
                    "→ Check Validity).")
    elif notes is not None:
        notes.append(f"«{name}»: {n} ta nuqta — o'zini kesishiga "
                     "tekshirilmadi, tekshiruv juda og'ir.")

    area = ring_area_ha(pts)
    rings = [ring]
    for h in (holes or []):
        hp = [[float(p[0]), float(p[1])] for p in h]
        hpts = from_geojson_ring(hp)
        if len(hpts) < 3:
            continue
        # Вырез вычитается, только если он ВНУТРИ своего поля. Кольцо,
        # лежащее рядом или накрывающее внешнее, — мусор экспорта либо
        # чужая геометрия, а вычесть его значит отнять у поля гектары,
        # которые никто не вырезал: в прогоне поле худело с 95 до 80 га,
        # а вырез больше кольца уводил площадь в минус (-2286 га).
        if not _point_inside([_closed(ring)], hp[0][0], hp[0][1]):
            if notes is not None:
                notes.append(f"«{name}»: ichki kontur dala ichida emas — "
                             "hisobga olinmadi.")
            continue
        area -= ring_area_ha(hpts)
        rings.append(_closed(hp))
    if not AREA_MIN_HA <= area <= AREA_MAX_HA:
        cut = (f" (ichki kontur: {len(rings) - 1})" if len(rings) > 1 else "")
        return (f"«{name}»: maydon {area:.2f} ga — "
                f"{AREA_MIN_HA}–{AREA_MAX_HA} ga oralig'idan tashqarida{cut}.")
    lat_c, lon_c = _field_point(rings)
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
