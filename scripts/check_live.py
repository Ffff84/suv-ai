#!/usr/bin/env python3
"""
Диагностика живых подключений.

Запуск:
    python scripts/check_live.py

Проверяет по очереди всё, что должно работать снаружи: погоду, спутник и
Telegram. Ничего не меняет — только смотрит и говорит, что именно сломано
и что с этим делать.

Правило этого файла: не «ошибка соединения», а «зайдите туда-то и сделайте
то-то». Диагностика, которая не подсказывает следующий шаг, бесполезна.
"""

from __future__ import annotations

import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from suv.config import describe, load_env

_LOADED = load_env()

OK, FAIL, SKIP = "  ОК  ", " СБОЙ ", "ПРОПУСК"


def line(status: str, title: str, detail: str = "") -> None:
    print(f"[{status}] {title}")
    if detail:
        for row in detail.strip().split("\n"):
            print(f"         {row}")


def check_weather(lat: float, lon: float) -> bool:
    """Open-Meteo. Ключ не нужен — если это не работает, значит нет сети."""
    try:
        from suv.weather import fetch_forecast
        wx = fetch_forecast(lat, lon, days=7)
    except Exception as exc:  # noqa: BLE001
        line(FAIL, "Погода — Open-Meteo", f"{type(exc).__name__}: {exc}\n"
             "Ключ здесь не нужен. Если не работает — проблема в интернете\n"
             "или api.open-meteo.com заблокирован в вашей сети.")
        return False

    d = wx[0]
    from suv.et0 import et0
    ref, method = et0(d, lat, 700)
    line(OK, "Погода — Open-Meteo",
         f"Получено дней: {len(wx)}\n"
         f"Завтра: {d.t_min:.0f}…{d.t_max:.0f}°C, "
         f"осадки {d.rainfall:.1f} мм, ET0 {ref:.1f} мм ({method})")
    return True


def check_satellite(lat: float, lon: float) -> bool:
    """Copernicus. Нужны CDSE_CLIENT_ID и CDSE_CLIENT_SECRET."""
    if not os.environ.get("CDSE_CLIENT_ID") or not os.environ.get("CDSE_CLIENT_SECRET"):
        line(SKIP, "Спутник — Copernicus Sentinel-2",
             "Не вижу CDSE_CLIENT_ID / CDSE_CLIENT_SECRET.\n"
             f"Проверено: {describe()}\n"
             "Файл .env должен лежать в корне проекта, рядом с README.md,\n"
             "и содержать две строки без пробелов вокруг знака =:\n"
             "    CDSE_CLIENT_ID=sh-...\n"
             "    CDSE_CLIENT_SECRET=...\n"
             "Если ключей ещё нет: dataspace.copernicus.eu → Sign in →\n"
             "меню профиля → Sentinel Hub → OAuth clients → Create.")
        return False

    try:
        from suv.satellite import bbox_polygon, fetch_ndvi, get_token
        token = get_token()
    except Exception as exc:  # noqa: BLE001
        line(FAIL, "Спутник — авторизация Copernicus",
             f"{type(exc).__name__}: {exc}\n"
             "Чаще всего это неверный CLIENT_SECRET или ещё не созданный\n"
             "OAuth-клиент в разделе Sentinel Hub.")
        return False
    line(OK, "Спутник — авторизация Copernicus", f"Токен получен ({len(token)} симв.)")

    try:
        r = fetch_ndvi(bbox_polygon(lat, lon, 200), token, window_days=12)
    except Exception as exc:  # noqa: BLE001
        line(FAIL, "Спутник — снимок NDVI",
             f"{type(exc).__name__}: {exc}\n"
             "Если ругается на rasterio — установите: pip install rasterio")
        return False

    if r is None:
        line(OK, "Спутник — снимок NDVI",
             "Ответ получен, но за 12 дней все снимки закрыты облаками.\n"
             "Это НЕ ошибка: движок в таком случае работает по календарю\n"
             "культуры. Попробуйте окно 20 дней.")
    else:
        line(OK, "Спутник — снимок NDVI",
             f"NDVI = {r.value:.3f}, чистых пикселей {r.valid_fraction*100:.0f}%")
    return True


def check_telegram() -> bool:
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        line(SKIP, "Telegram — бот",
             "Не вижу TELEGRAM_TOKEN.\n"
             f"Проверено: {describe()}\n"
             "Откройте @BotFather → /newbot → имя → username, который\n"
             "заканчивается на bot. Он вернёт токен вида 8123456:AAF...")
        return False
    try:
        import requests
        r = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=15)
        r.raise_for_status()
        me = r.json()["result"]
    except Exception as exc:  # noqa: BLE001
        line(FAIL, "Telegram — бот", f"{type(exc).__name__}: {exc}\n"
             "Проверьте, что токен скопирован целиком, вместе с цифрами до двоеточия.")
        return False
    line(OK, "Telegram — бот", f"@{me['username']} ({me['first_name']})")
    return True


def check_engine() -> bool:
    """Движок обязан работать вообще без интернета."""
    try:
        from suv.climate import STATIONS, season
        from suv.crop import CROPS
        from suv.messages import recommendation_text
        from suv.schedule import Field, recommend
        from suv.soil import SOILS, WaterBalanceState

        st = STATIONS["samarkand"]
        f = Field("TEST", "Test", 1.0, st.lat, st.lon, st.elevation_m,
                  CROPS["cotton"], SOILS["loam"], date(2026, 4, 10), "furrow",
                  water_table_depth_m=st.typical_water_table_m)
        rec = recommend(f, season(st, date(2026, 7, 12), 14),
                        WaterBalanceState(105.0, 1.35), date(2026, 7, 12))
        recommendation_text(rec, "uz")
    except Exception as exc:  # noqa: BLE001
        line(FAIL, "Движок расчёта", f"{type(exc).__name__}: {exc}")
        return False
    line(OK, "Движок расчёта", "Считает без интернета — как и должен")
    return True


def main() -> int:
    lat = float(os.environ.get("FIELD_LAT", "39.65"))
    lon = float(os.environ.get("FIELD_LON", "66.96"))

    print()
    print("  SUV AI — проверка живых подключений")
    if _LOADED:
        print(f"  Ключи из .env: {', '.join(sorted(_LOADED))}")
    else:
        print(f"  .env не прочитан ({describe()}) — смотрю только "
              f"переменные окружения")
    print(f"  Точка проверки: {lat:.4f}, {lon:.4f}")
    print("  " + "-" * 58)

    results = {
        "движок": check_engine(),
        "погода": check_weather(lat, lon),
        "спутник": check_satellite(lat, lon),
        "telegram": check_telegram(),
    }

    print("  " + "-" * 58)
    ready = results["движок"] and results["погода"]
    if ready and results["спутник"] and results["telegram"]:
        print("  Всё подключено. Можно запускать бота на реальном поле.")
    elif ready:
        missing = [k for k in ("спутник", "telegram") if not results[k]]
        print(f"  Основа работает. Не хватает: {', '.join(missing)}.")
        print("  Бот может работать и без спутника — по календарю культуры,")
        print("  чуть менее точно. Без Telegram фермер ничего не получит.")
    else:
        print("  Пока не готово. Начните с двух верхних строк.")
    print()
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
