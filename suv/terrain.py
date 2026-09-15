"""
Средний уклон поля по открытому рельефу Copernicus DEM GLO-30.

Зачем: модель добегания воды по борозде (ТЗ Egat) упирается в уклон, а
мерить его пока нечем — дрона нет, нивелиром никто не выезжал. Плоскость,
вписанная регрессией во ВСЕ пиксели рельефа внутри контура поля, даёт
общий градиент — величину и направление стока — без выезда и бесплатно.

Откуда рельеф: публичный бакет AWS Open Data `copernicus-dem-30m` —
GLO-30 лежит там открытыми COG-тайлами 1°x1°, rasterio читает нужное
окошко range-запросами, никаких ключей и регистраций. Путь через наш же
Sentinel Hub (CDSE) проверен и отвергнут: GLO-30 у них закрыт за
CCM-регистрацией (403 COMMON_INSUFFICIENT_PERMISSIONS, замер 15.09.2026),
а открытый GLO-90 на поле 200 м даёт 2x2 пикселя — не данные.

Это ТРИАЖ, а не замер: сортировка полей на «плоское / с заметным уклоном /
куда течёт». В движок расчёта отсюда не пишется ни одной цифры. Четыре
ограничения, каждое из которых об этом напоминает:

* GLO-30 снят радаром TanDEM-X в 2010–2015 гг. Поле, прошедшее лазерную
  планировку позже, здесь выглядит ДО планировки.
* Это модель ПОВЕРХНОСТИ, не земли: в саду и винограднике радар видел
  кроны, а не почву между рядами.
* Пиксель ~30 м: на поле в 2 га помещается порядка 50 пикселей.
  Регрессия усредняет шум, но стандартная ошибка остаётся долями
  процента — уклон слабее 2·SE честно объявляется неотличимым от нуля.
  Правило то же, что у field_photo: не рисовать узор, которого нет.
* Средний градиент — не профиль борозды: локальную яму, где вода встаёт
  и уходит в фильтрацию, он не видит в принципе. Профиль даст только
  нивелир или облёт дроном.

Сетевая часть (fetch_dem) в CI не гоняется — там нет сети; считать её
проверенной только после прогона scripts/field_slope.py, как satellite.py.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

# Порог выведен экспериментом, а не взят с потолка: на сетке 4x4
# (поле 120 м, как Uzumzor) чистый шум 1 м в трети прогонов рисует
# «уверенные» 2% с прошедшим порог 2·SE — на 13 степенях свободы SE сама
# оценивается слишком грубо, и ворота не держат. С 5x5 = 25 пикселей
# (поле от ~150 м) ложная тревога падает до терпимой. Мелкому полю
# скрипт советует --expand, а не выдаёт лотерейную цифру.
MIN_PIXELS = 25

# Ниже этого уклона (5 см на 100 м) GLO-30 не имеет права ничего
# утверждать независимо от SE: на синтетике без шума SE обнуляется, и
# без пола «различимым» становился float-мусор порядка 1e-15%.
MIN_SLOPE_PCT = 0.05

# Румбы — полными словами, как water_inlet в fields/*.json («северо-восток»).
RUMBS = ("север", "северо-восток", "восток", "юго-восток",
         "юг", "юго-запад", "запад", "северо-запад")

# COG-тайлы GLO-30 в реестре открытых данных AWS: 1°x1°, имя — юго-западный
# угол. «DSM_COG_10» в имени — код разрешения 1 угл. секунда (~30 м),
# у 90-метрового собрата код 30 и свой бакет copernicus-dem-90m.
_TILE_URL = ("https://copernicus-dem-30m.s3.amazonaws.com/"
             "Copernicus_DSM_COG_10_{ns}{lat:02d}_00_{ew}{lon:03d}_00_DEM/"
             "Copernicus_DSM_COG_10_{ns}{lat:02d}_00_{ew}{lon:03d}_00_DEM.tif")

# Море и провалы съёмки в GLO-30 — большие отрицательные заглушки;
# на суше Узбекистана всё живое лежит выше впадин Прикаспия (-132 м).
_ELEV_FLOOR_M = -500.0


@dataclass
class SlopeEstimate:
    slope_pct: float   # величина среднего уклона, % (1% = 1 м на 100 м)
    se_pct: float      # стандартная ошибка уклона, %
    aspect_deg: float  # азимут стока — куда потечёт вода; 0 = север
    n_pixels: int      # пикселей рельефа внутри контура
    elev_mean: float   # средняя высота, м
    elev_span: float   # перепад max-min по пикселям, м

    @property
    def detectable(self) -> bool:
        """Уклон различим на фоне шума GLO-30 (порог 2·SE и физический пол)."""
        return (self.slope_pct >= MIN_SLOPE_PCT
                and self.slope_pct >= 2.0 * self.se_pct)

    @property
    def downhill(self) -> str:
        return rumb(self.aspect_deg)


def rumb(deg: float) -> str:
    """Азимут -> ближайший из восьми румбов."""
    return RUMBS[int(round((deg % 360.0) / 45.0)) % 8]


def fit_plane(z, inside, dx_m: float, dy_m: float) -> SlopeEstimate | None:
    """Плоскость по пикселям рельефа: z = a + b·восток + c·север.

    z — растр высот (строка 0 = северный край, как в GeoTIFF), inside —
    маска «пиксель внутри контура», dx_m/dy_m — шаг пикселя в метрах.
    Чистая математика без сети и rasterio — чтобы порог MIN_PIXELS и
    правило 2·SE были под тестами, как reduce_index_arrays.

    None — когда пикселей меньше MIN_PIXELS или геометрия вырождена
    (все в одной строке/столбце): отказ, а не уверенная цифра.
    """
    z = np.asarray(z, dtype=float)
    inside = np.asarray(inside, dtype=bool)
    row_idx, col_idx = np.nonzero(inside)
    n = int(row_idx.size)
    if n < MIN_PIXELS:
        return None

    x = col_idx * dx_m          # на восток
    y = -(row_idx * dy_m)       # строка растёт к югу, ось y — на север
    x = x - x.mean()
    y = y - y.mean()
    a_mat = np.column_stack([np.ones(n), x, y])
    zv = z[inside]
    try:
        coef, *_ = np.linalg.lstsq(a_mat, zv, rcond=None)
        ata_inv = np.linalg.inv(a_mat.T @ a_mat)
    except np.linalg.LinAlgError:
        return None

    resid = zv - a_mat @ coef
    sigma2 = float(resid @ resid) / (n - 3)
    cov = sigma2 * ata_inv
    b, c = float(coef[1]), float(coef[2])
    vb, vc, vbc = float(cov[1, 1]), float(cov[2, 2]), float(cov[1, 2])

    grad = math.hypot(b, c)
    if grad < 1e-12:
        # Идеально плоско (бывает на синтетике): направление не определено.
        se = math.sqrt(max(vb, vc))
        aspect = 0.0
    else:
        # Ошибка величины градиента — проекцией ковариации на его направление.
        se = math.sqrt(max(0.0, b * b * vb + c * c * vc + 2 * b * c * vbc)) / grad
        # Градиент (b, c) смотрит вверх; сток — противоположно.
        aspect = math.degrees(math.atan2(-b, -c)) % 360.0

    return SlopeEstimate(slope_pct=grad * 100.0, se_pct=se * 100.0,
                         aspect_deg=aspect, n_pixels=n,
                         elev_mean=float(zv.mean()),
                         elev_span=float(zv.max() - zv.min()))


def gravity_check(water_inlet: str, aspect_deg: float) -> bool | None:
    """Согласуется ли вход воды с рельефом: заходить она должна с верхней
    стороны поля. Допуск ±67,5° (полтора румба): и обвод контура, и
    вписанная плоскость грубые, придираться к соседнему румбу нечестно.
    None — сторона входа не распознана как румб."""
    name = water_inlet.strip().lower().replace("ё", "е")
    if name not in RUMBS:
        return None
    inlet_deg = RUMBS.index(name) * 45.0
    uphill = (aspect_deg + 180.0) % 360.0
    diff = abs((inlet_deg - uphill + 180.0) % 360.0 - 180.0)
    return diff <= 67.5


def tile_url(ring: list[list[float]]) -> str:
    """URL тайла GLO-30, накрывающего контур (контур — lon/lat).

    Контур на стыке листов — отказ с объяснением, а не тихое чтение
    половины поля: склейка тайлов не написана, и пока ни одно поле её
    не попросило, честнее сказать это вслух.
    """
    lons = [p[0] for p in ring]
    lats = [p[1] for p in ring]
    corners = {(math.floor(la), math.floor(lo))
               for la in (min(lats), max(lats))
               for lo in (min(lons), max(lons))}
    if len(corners) > 1:
        raise RuntimeError(
            "контур лёг на стык листов DEM (границу целого градуса) — "
            "склейка тайлов не реализована; сдвиньте квадрат или "
            "уменьшите --expand")
    (la, lo), = corners
    return _TILE_URL.format(ns="N" if la >= 0 else "S", lat=abs(la),
                            ew="E" if lo >= 0 else "W", lon=abs(lo))


def points_in_ring(lon_g, lat_g, ring: list[list[float]]):
    """Маска «точка внутри контура» лучом (even-odd), чистый numpy —
    shapely ради одного полигона в зависимости не тянем."""
    lon_g = np.asarray(lon_g, dtype=float)
    lat_g = np.asarray(lat_g, dtype=float)
    inside = np.zeros(lon_g.shape, dtype=bool)
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = float(ring[i][0]), float(ring[i][1])
        xj, yj = float(ring[j][0]), float(ring[j][1])
        j = i
        if yi == yj:  # горизонтальное (и вырожденное) ребро луч не режет
            continue
        hit = (((yi > lat_g) != (yj > lat_g))
               & (lon_g < (xj - xi) * (lat_g - yi) / (yj - yi) + xi))
        inside ^= hit
    return inside


def fetch_dem(polygon: list[list[float]]):
    """Окно GLO-30 над контуром поля из открытого бакета AWS.

    Возвращает (z, inside, dx_m, dy_m) для fit_plane — родные пиксели
    тайла, без передискретизации: интерполированные пиксели не
    независимы, они занижают SE, и порог 2·SE начинает «видеть» уклоны,
    которых нет. Шаг по долготе на этой широте ~24 м, по широте ~31 м —
    метры считаются из геотрансформа, не предполагаются.
    """
    try:
        import rasterio
        from rasterio.windows import from_bounds
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError("fetch_dem needs rasterio installed") from exc

    lons = [p[0] for p in polygon]
    lats = [p[1] for p in polygon]
    url = "/vsicurl/" + tile_url(polygon)
    # EMPTY_DIR: иначе GDAL пытается листать «каталог» рядом с тайлом
    # и на каждое поле делает лишние запросы к бакету.
    with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR"):
        with rasterio.open(url) as src:
            win = from_bounds(min(lons), min(lats), max(lons), max(lats),
                              src.transform)
            win = win.round_lengths().round_offsets()
            z = src.read(1, window=win).astype("float32")
            t = src.window_transform(win)

    rows, cols = np.mgrid[0:z.shape[0], 0:z.shape[1]]
    lon_c = t.c + t.a * (cols + 0.5)
    lat_c = t.f + t.e * (rows + 0.5)
    inside = points_in_ring(lon_c, lat_c, polygon)
    inside &= np.isfinite(z) & (z > _ELEV_FLOOR_M)

    lat_mid = (min(lats) + max(lats)) / 2.0
    dx_m = abs(t.a) * 111_320.0 * max(0.2, abs(math.cos(math.radians(lat_mid))))
    dy_m = abs(t.e) * 111_320.0
    return z, inside, dx_m, dy_m
