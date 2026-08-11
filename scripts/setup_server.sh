#!/usr/bin/env bash
# Первичная настройка чистого Ubuntu-сервера под бота.
#
# Запуск на СЕРВЕРЕ, одной строкой:
#   curl -fsSL https://raw.githubusercontent.com/Ffff84/suv-ai/main/scripts/setup_server.sh | bash
#
# Можно запускать повторно: ничего не ломает, .env не перезаписывает.
# Ключи скрипт не знает и знать не может — их вписывает человек.
set -euo pipefail

DIR=/root/suv-ai
REPO=https://github.com/Ffff84/suv-ai.git
# chat_id фермера-владельца полей. Задаётся при запуске:
#   curl -fsSL .../setup_server.sh | FARMER_CHAT_ID=123456789 bash
# Не задан — скрипт всё настроит, но поля к чату не привяжет.
FARMER_CHAT_ID="${FARMER_CHAT_ID:-}"

echo "== 1/5 пакеты =="
apt-get update -qq
apt-get install -y -qq python3-pip git

echo "== 2/5 код =="
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" fetch origin main && git -C "$DIR" reset --hard origin/main
else
  git clone -q "$REPO" "$DIR"
fi
pip install -r "$DIR/requirements.txt" --break-system-packages -q

echo "== 3/5 .env =="
if [ -f "$DIR/.env" ]; then
  echo "   .env уже есть — не трогаю"
else
  cat > "$DIR/.env" <<'EOF'
# Заполните и сохраните. Этот файл НИКОГДА не попадает в git.
TELEGRAM_TOKEN=
CDSE_CLIENT_ID=
CDSE_CLIENT_SECRET=
# Пусто = бот открыт всем (демо). Для пилота впишите chat_id через запятую.
ALLOWED_CHAT_IDS=
OBSERVER_CHAT_IDS=
EOF
  chmod 600 "$DIR/.env"
  echo "   создан шаблон $DIR/.env"
fi

echo "== 4/5 поля =="
cd "$DIR"
if [ -n "$FARMER_CHAT_ID" ]; then
  python3 scripts/seed_field.py fields/olma.json --chat "$FARMER_CHAT_ID" >/dev/null
  python3 scripts/seed_field.py fields/uzum.json --chat "$FARMER_CHAT_ID" >/dev/null
  echo "   Olmazor и Uzumzor записаны на chat_id $FARMER_CHAT_ID"
else
  echo "   FARMER_CHAT_ID не задан — поля не привязаны. Позже:"
  echo "   FARMER_CHAT_ID=123456789 bash $DIR/scripts/setup_server.sh"
fi

echo "== 5/5 служба =="
cat > /etc/systemd/system/suv-ai.service <<'EOF'
[Unit]
Description=SUV AI bot
After=network-online.target

[Service]
WorkingDirectory=/root/suv-ai
ExecStart=/usr/bin/python3 -m bot.main
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable -q suv-ai

# Без токена служба будет циклически падать и засорять логи — честнее
# не запускать её вовсе и сказать об этом прямо.
if grep -q '^TELEGRAM_TOKEN=.\+' "$DIR/.env"; then
  systemctl restart suv-ai
  sleep 3
  systemctl is-active suv-ai && echo
  echo "Готово. Бот запущен."
  echo "Живой лог: journalctl -u suv-ai -f"
else
  echo
  echo "Осталось одно: вписать ключи."
  echo "   nano $DIR/.env       # TELEGRAM_TOKEN и пара CDSE_*"
  echo "   systemctl start suv-ai"
  echo
  echo "Служба создана и включена в автозапуск, но НЕ запущена:"
  echo "без TELEGRAM_TOKEN бот всё равно не поднимется."
fi

echo
echo "Не забудьте остановить бота на ноутбуке — Telegram разрешает"
echo "только одно подключение на токен."
