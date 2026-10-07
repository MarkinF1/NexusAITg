#!/usr/bin/env bash
# Ставит и запускает бота как пользовательский systemd-сервис.
# Разовый запуск в текущем терминале: .venv/bin/python bot.py
set -euo pipefail
cd "$(dirname "$0")"
# пути в claude-tg.service жёсткие
[ "$PWD" = /opt/NexusAI ] || { echo "проект должен лежать в /opt/NexusAI, а не в $PWD" >&2; exit 1; }

[ -f .env ] || { echo "нет .env рядом со скриптом (нужны TG_TOKEN и TG_ALLOWED)" >&2; exit 1; }
[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
# модель распознавания качается один раз (~0.5 ГБ); иначе ждать пришлось бы
# на первом же голосовом
.venv/bin/python -c "
import os
from faster_whisper import WhisperModel
WhisperModel(os.environ.get('CLAUDE_TG_STT_MODEL', 'small'), device='cpu', compute_type='int8')"

mkdir -p ~/.config/systemd/user
ln -sf "$PWD/claude-tg.service" ~/.config/systemd/user/claude-tg.service
systemctl --user daemon-reload
systemctl --user enable --now claude-tg.service
loginctl enable-linger "$USER" 2>/dev/null || echo "не смог включить linger — сервис не поднимется до входа в систему"

systemctl --user --no-pager status claude-tg.service | head -5
echo "логи: journalctl --user -u claude-tg -f"
