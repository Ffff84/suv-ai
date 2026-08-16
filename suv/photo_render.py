"""
Сборка кадра: снимок, заливка по влажности, контур, легенда, подпись.

Отделено от suv/field_photo.py (правила и цвета) и suv/scene.py (сеть):
рисование требует Pillow, а правила должны проверяться тестами без него.
"""

from __future__ import annotations

from datetime import date

from .field_photo import (MoistureStats, bbox_of, moisture_color,
                          ring_to_pixels, scale_bar_m, stats_over_field)

WIDTH = 1080          # ТЗ §4.1
# Апскейл всегда NEAREST. ТЗ называет кратность 6-8, но она выведена для
# поля покрупнее: участок в 3 га даёт растр всего в 22 пикселя, и такая
# кратность оставила бы картинку шириной 176 точек. Тянем до 1080 —
# блочность от этого только заметнее, а она и есть честный показ того,
# что один квадрат на снимке равен десяти метрам поля.
MIN_UPSCALE = 6
# Telegram отказывается принимать фото с отношением сторон круче 1:20,
# а на телефоне такая полоса всё равно нечитаема.
MAX_HEIGHT = 1920

INK = (238, 238, 238)
DIM = (170, 170, 170)
PANEL = (16, 16, 16)


def _font(size: int):
    """Шрифт с кириллицей и латиницей. Без него подписи станут квадратами."""
    from PIL import ImageFont
    for name in ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _polygon_mask(ring_px, size):
    """Маска поля: за контуром не красим ничего (ТЗ §4.3)."""
    from PIL import Image, ImageDraw
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).polygon([(x, y) for x, y in ring_px], fill=255)
    return mask


def _inside_grid(ring_px, w: int, h: int) -> list[list[bool]]:
    """Какие пиксели растра лежат внутри контура."""
    from PIL import Image
    mask = _polygon_mask(ring_px, (w, h))
    px = mask.load()
    return [[px[x, y] > 0 for x in range(w)] for y in range(h)]


def measure(scene, ring: list[list[float]]):
    """Замер влажности внутри контура — без сборки картинки.

    Нужен, чтобы выбрать кадр: годность решается долей чистых пикселей
    ВНУТРИ поля (ТЗ §4.2), а её видно только когда кадр уже скачан.
    """
    w0, h0 = scene.rgb.size
    ring_px = ring_to_pixels(ring, bbox_of(ring), w0, h0)
    inside = _inside_grid(ring_px, w0, h0)
    return stats_over_field(scene.ndmi, inside, scene.valid)


def build(scene, ring: list[list[float]], *, field_name: str,
          area_ha: float | None, today: date | None = None,
          lang: str = "uz", contour_is_real: bool = True):
    """Собрать фото поля. Возвращает (изображение PIL, подпись, статистика).

    contour_is_real=False — контур поля ещё не обведён, и показан квадрат
    вокруг точки. Такой кадр честно об этом пишет прямо на картинке:
    в квадрат попадают соседние участки, и принимать по нему решения
    о своём поле нельзя.
    """
    from PIL import Image, ImageChops, ImageDraw

    today = today or date.today()
    uz = lang == "uz"
    box = bbox_of(ring)
    base = scene.rgb
    w0, h0 = base.size

    ring_px = ring_to_pixels(ring, box, w0, h0)
    inside = _inside_grid(ring_px, w0, h0)
    stats = stats_over_field(scene.ndmi, inside, scene.valid)

    # Заливка в нативной сетке, апскейл — потом и только NEAREST: иначе
    # сглаживание нарисует плавные переходы, которых в данных нет.
    overlay = Image.new("RGBA", (w0, h0), (0, 0, 0, 0))
    if stats is not None and not stats.uniform:
        opx = overlay.load()
        for y in range(h0):
            for x in range(w0):
                if scene.valid[y][x]:
                    opx[x, y] = moisture_color(scene.ndmi[y][x], stats)

    # Кратность считаем и по высоте тоже. По одной ширине узкое поле
    # вдоль канала давало картинку 1080 на четырнадцать тысяч точек —
    # Telegram такую не примет, а фермер на телефоне не разглядит.
    scale = max(MIN_UPSCALE, min(-(-WIDTH // max(1, w0)),
                                 -(-MAX_HEIGHT // max(1, h0))))
    size = (w0 * scale, h0 * scale)
    img = base.resize(size, Image.NEAREST).convert("RGBA")

    # Режем заливку по контуру УЖЕ после апскейла. Маска, построенная в
    # нативной сетке, округляется до целой клетки — а клетка это десять
    # метров поля, и на готовом кадре заливка вылезала за межу на полосу
    # в полсотни пикселей, закрашивая соседский участок.
    big = overlay.resize(size, Image.NEAREST)
    fine = _polygon_mask([(x * scale, y * scale) for x, y in ring_px], size)
    big.putalpha(ImageChops.multiply(big.getchannel("A"), fine))
    img.alpha_composite(big)
    img = img.convert("RGB")

    d = ImageDraw.Draw(img)
    poly = [(x * scale, y * scale) for x, y in ring_px]
    d.line(poly + [poly[0]], fill=(255, 255, 255), width=3)

    _north(d, img.width, _font(16), uz)
    _scale_bar(d, img, box, _font(15))
    if not contour_is_real:
        _warn_strip(img, uz)

    caption = _caption(scene.day, today, stats, field_name, area_ha, uz,
                       contour_is_real)
    return _with_legend(img, stats, uz), caption, stats


def _north(d, width: int, font, uz: bool) -> None:
    x, y = width - 46, 18
    d.line([(x, y + 34), (x, y)], fill=(255, 255, 255), width=3)
    d.polygon([(x - 7, y + 9), (x + 7, y + 9), (x, y - 3)], fill=(255, 255, 255))
    d.text((x - 5, y + 38), "N" if uz else "С", fill=(255, 255, 255), font=font)


def _scale_bar(d, img, box, font) -> None:
    metres = scale_bar_m(box)
    lon0, _lat0, lon1, _lat1 = box
    px_per_deg = img.width / (lon1 - lon0)
    import math
    mid = math.radians((box[1] + box[3]) / 2.0)
    bar = int(metres / (111_320.0 * math.cos(mid)) * px_per_deg)
    x, y = 20, img.height - 30
    d.rectangle([x, y, x + bar, y + 7], fill=(255, 255, 255))
    d.text((x, y - 20), f"{metres} m", fill=(255, 255, 255), font=font)


def _warn_strip(img, uz: bool) -> None:
    """Полоса поверх кадра: контур не обведён, это не границы поля."""
    from PIL import Image, ImageDraw
    band = Image.new("RGBA", (img.width, 42), (150, 30, 30, 220))
    img.paste(Image.alpha_composite(
        img.crop((0, 0, img.width, 42)).convert("RGBA"), band).convert("RGB"),
        (0, 0))
    ImageDraw.Draw(img).text(
        (14, 12),
        "DIQQAT: dala chegarasi chizilmagan - bu nuqta atrofidagi kvadrat"
        if uz else
        "ВНИМАНИЕ: контур поля не обведён — это квадрат вокруг точки",
        fill=(255, 240, 240), font=_font(17))


def _with_legend(img, stats: MoistureStats | None, uz: bool):
    """Полоса снизу с легендой — ТЗ §4.4, цвет всегда со словом."""
    from PIL import Image, ImageDraw
    if stats is None or stats.uniform:
        return img
    font, small = _font(19), _font(16)
    title = ("Rang - o'lchangan namlik" if uz
             else "Цвет — измеренная влажность")
    left_word = "quruqroq" if uz else "суше"
    right_word = "namroq" if uz else "влажнее"

    d0 = ImageDraw.Draw(img)
    title_w = int(d0.textlength(title, font=font))
    pad, gap = 20, 24
    # Ширину шкалы считаем от ширины надписи, а не вычитаем зашитые 300
    # точек: русская подпись длиннее узбекской и обрезалась на любом кадре.
    bar_w = max(120, img.width - title_w - 2 * pad - gap)
    h = 64
    out = Image.new("RGB", (img.width, img.height + h), PANEL)
    out.paste(img, (0, 0))
    d = ImageDraw.Draw(out)

    bar_h = 16
    x0, y0 = pad, img.height + 14
    for i in range(bar_w):
        t = i / max(1, bar_w - 1)
        r, g, b, a = moisture_color(stats.low + t * stats.spread, stats)
        # Смешиваем с фоном полосы тем же альфа-каналом, что и заливка на
        # карте. Непрозрачный образец показывал цвет, которого на поле
        # нет ни одного пикселя, и легенда не сходилась с картинкой.
        k = a / 255.0
        mixed = tuple(int(c * k + p * (1 - k)) for c, p in
                      ((r, PANEL[0]), (g, PANEL[1]), (b, PANEL[2])))
        d.line([(x0 + i, y0), (x0 + i, y0 + bar_h)], fill=mixed)

    d.text((x0, y0 + bar_h + 6), left_word, fill=DIM, font=small)
    rw = int(d.textlength(right_word, font=small))
    d.text((x0 + bar_w - rw, y0 + bar_h + 6), right_word, fill=DIM, font=small)
    d.text((x0 + bar_w + gap, y0 - 2), title, fill=INK, font=font)
    return out


def _caption(scene_day: date | None, today: date, stats: MoistureStats | None,
             field_name: str, area_ha: float | None, uz: bool,
             contour_is_real: bool) -> str:
    """Подпись под фото. Дата снимка обязательна (ТЗ §1.2)."""
    lines: list[str] = []
    if scene_day:
        ago = (today - scene_day).days
        when = ("bugun" if ago == 0 else "kecha" if ago == 1
                else f"{ago} kun oldin") if uz else (
            "сегодня" if ago == 0 else "вчера" if ago == 1
            else f"{ago} дн. назад")
        lines.append(f"🛰 {'Surat' if uz else 'Снимок'}: "
                     f"{scene_day.day:02d}.{scene_day.month:02d} ({when})")
    lines.append("")

    if stats is None:
        lines.append("Namlik o'lchanmadi — bulut." if uz
                     else "Влажность не измерена — облачность.")
    elif stats.uniform:
        # Ровное поле не красим: растянутая шкала нарисовала бы узор,
        # которого в данных нет.
        #
        # И говорим только про РОВНОСТЬ. Прежняя формулировка добавляла
        # «сухих зон не видно» — а поле, сплошь пересохшее, тоже ровное,
        # и фермер получал бы успокоение вместо тревоги. Насколько поле
        # влажное вообще, по одному снимку сказать нельзя: порог зависит
        # от культуры и стадии.
        lines.append("Namlik dala bo'ylab bir xil: quruq va nam joylarga "
                     "bo'linmagan." if uz else
                     "Влажность по полю ровная: на сухие и влажные зоны "
                     "не делится.")
    else:
        # Цвет всегда со словом, и слово — сравнительное. NDMI падает и
        # от сухости, и от изреженности растений, поэтому «суше» здесь
        # честнее, чем «сухо».
        lines.append("Qizil — quruqroq joy" if uz else "Красный — суше")
        lines.append("Yashil — namroq joy" if uz else "Зелёный — влажнее")

    if stats is not None:
        lines.append("")
        lines.append(
            f"📊 {'Tekshirilgan' if uz else 'Измерено'}: "
            f"{stats.valid_fraction * 100:.0f}% "
            f"{'dala yuzasi' if uz else 'площади поля'}")

    if not contour_is_real:
        lines.append("")
        lines.append("⚠️ Dala chegarasi chizilmagan: kvadratga qo'shni "
                     "yerlar ham tushgan." if uz else
                     "⚠️ Контур поля не обведён: в квадрат попали соседние "
                     "участки.")
    return "\n".join(lines)
