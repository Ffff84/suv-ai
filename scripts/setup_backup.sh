#!/usr/bin/env bash
#
# Ночная копия журнала и добивание Landsat на живом сервере.
#
# Запускается НЕ на сервере руками, а подаётся ему на вход, чтобы не
# пересобирать команду через кавычки PowerShell → ssh → bash (именно на
# этом развалилась первая попытка: `crontab -` получил битую строку):
#
#   type scripts\setup_backup.sh | ssh root@suv-ai.online "tr -d '\r' | bash -s"
#
# tr -d '\r' обязателен: git на Windows выдаёт файл с CRLF, а bash от
# возврата каретки спотыкается на каждой строке.
#
# Повторный запуск безопасен: строка LANDSAT_FALLBACK переписывается, а
# не добавляется второй раз, задание cron перезаписывается целиком.
set -euo pipefail

DIR=/root/suv-ai
ENVF="$DIR/.env"
DEST=/root/backup

echo "== 1/4 .env =="
if [ -f "$ENVF" ]; then
    # Первая попытка успела дописать строку до того, как упала на
    # crontab, поэтому сначала вычищаем все прежние вхождения.
    tmp=$(mktemp)
    grep -v '^LANDSAT_FALLBACK=' "$ENVF" > "$tmp" || true
    echo 'LANDSAT_FALLBACK=1' >> "$tmp"
    cat "$tmp" > "$ENVF"
    rm -f "$tmp"
    chmod 600 "$ENVF"
    echo "   LANDSAT_FALLBACK=1 (вхождений: $(grep -c '^LANDSAT_FALLBACK=' "$ENVF"))"
else
    echo "   ВНИМАНИЕ: $ENVF не найден — Landsat не включён"
fi

echo "== 2/4 каталог копий =="
mkdir -p "$DEST"
chmod 700 "$DEST"
echo "   $DEST"

echo "== 3/4 задание =="
# Копия делается python-модулем sqlite3, а не утилитой sqlite3: CLI на
# сервере может не стоять, а python3 стоит гарантированно — на нём
# работает сам бот. И .backup, а не cp: копия живой базы обычным
# копированием бьётся, если в этот момент шла запись.
cat > /usr/local/bin/suv-backup <<'PY'
#!/usr/bin/env python3
"""Горячая копия журнала SUV AI.

Журнал — единственный носитель KPI и акта экономии с Фаррухом. До
09.09.2026 он жил в одном экземпляре на одной машине.

Это защита от повреждения базы и от случайного удаления, но НЕ от
потери самого сервера: копии лежат рядом с оригиналом. Вынос за пределы
машины — следующий шаг, и пока он не сделан, говорить «журнал
зарезервирован» можно только с этой оговоркой.
"""
import sqlite3
import sys
import time
from pathlib import Path

DB = Path("/root/suv-ai/suv.db")
DEST = Path("/root/backup")
KEEP_DAYS = 14

if not DB.exists():
    print(f"{DB} не найден — копировать нечего")
    sys.exit(0)

DEST.mkdir(parents=True, exist_ok=True)
out = DEST / f"suv-{time.strftime('%Y-%m-%d')}.db"

src = sqlite3.connect(DB)
dst = sqlite3.connect(out)
with dst:
    src.backup(dst)
dst.close()
src.close()

cutoff = time.time() - KEEP_DAYS * 86400
dropped = 0
for old in DEST.glob("suv-*.db"):
    if old.stat().st_mtime < cutoff:
        old.unlink()
        dropped += 1

print(f"{out} — {out.stat().st_size} байт, удалено старых: {dropped}")
PY
chmod +x /usr/local/bin/suv-backup

printf '%s\n' \
    '# Ночная копия журнала SUV AI. См. scripts/setup_backup.sh в репозитории.' \
    'SHELL=/bin/sh' \
    'PATH=/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin' \
    '0 3 * * * root /usr/local/bin/suv-backup' \
    > /etc/cron.d/suv-backup
chmod 644 /etc/cron.d/suv-backup
echo "   /etc/cron.d/suv-backup — ежедневно в 03:00"

echo "== 4/4 проверка =="
/usr/local/bin/suv-backup
ls -lh "$DEST" | tail -3
systemctl restart suv-ai
sleep 3
if journalctl -u suv-ai -n 60 --no-pager | grep -i landsat; then
    echo "   Landsat взведён"
else
    echo "   строки про Landsat в логе НЕТ — проверьте .env и лог вручную"
fi
echo "готово"
