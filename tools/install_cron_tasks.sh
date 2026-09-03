#!/usr/bin/env bash
set -euo pipefail

# Устанавливает cron-задачу для scheduled_pipeline.py
# Времена и TZ берёт из .env:
#   PIPELINE_SCHEDULE_TIMES=09:00,18:00,21:00
#   PIPELINE_SCHEDULE_TZ=Asia/Yekaterinburg
# Если не заданы, используются значения по умолчанию.
#
# Запуск:
#   bash ./install_cron_tasks.sh
# Удаление:
#   bash ./install_cron_tasks.sh --remove
#
# После установки смотрите логи:
#   tail -f ./data/logs/scheduled_pipeline.log

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"
LOG_DIR="$REPO_ROOT/data/logs"
LOG_FILE="$LOG_DIR/scheduled_pipeline.log"
LOCK_FILE="$REPO_ROOT/data/scheduled_pipeline.lock"
ENV_FILE="$REPO_ROOT/.env"

if [[ ! -f "$REPO_ROOT/autobot/scheduled_pipeline.py" ]]; then
  echo "Не найден $REPO_ROOT/autobot/scheduled_pipeline.py" >&2
  exit 1
fi

mkdir -p "$LOG_DIR"

if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC2163
  while IFS= read -r line; do
    [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
    if [[ "$line" =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*=(.*)$ ]]; then
      key="${BASH_REMATCH[1]}"
      val="${BASH_REMATCH[2]}"
      val="${val%\"}"; val="${val#\"}"
      val="${val%\'}"; val="${val#\'}"
      if [[ -z "${!key:-}" ]]; then
        export "$key=$val"
      fi
    fi
  done < "$ENV_FILE"
fi

if command -v python3 >/dev/null 2>&1; then
  PY_CMD="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
  PY_CMD="$(command -v python)"
else
  echo "Не найден python3/python в PATH" >&2
  exit 1
fi

if ! command -v flock >/dev/null 2>&1; then
  echo "Не найден flock (util-linux): без него cron-задача могла бы запускаться параллельно." >&2
  exit 1
fi
FLOCK_CMD="$(command -v flock)"

START_MARK="# >>> AUTO_BOT_EIS_PIPELINE >>>"
END_MARK="# <<< AUTO_BOT_EIS_PIPELINE <<<"
TZ_VALUE="${PIPELINE_SCHEDULE_TZ:-Asia/Yekaterinburg}"
TIMES_RAW="${PIPELINE_SCHEDULE_TIMES:-09:00,18:00,21:00}"

IFS=',' read -r -a TIMES <<< "$TIMES_RAW"
CRON_LINES=()
for t in "${TIMES[@]}"; do
  t="$(echo "$t" | xargs)"
  if [[ ! "$t" =~ ^([0-9]{1,2}):([0-9]{2})$ ]]; then
    continue
  fi
  hh="${BASH_REMATCH[1]}"
  mm="${BASH_REMATCH[2]}"
  CRON_LINES+=("$((10#$mm)) $((10#$hh)) * * * cd \"$REPO_ROOT\" && \"$FLOCK_CMD\" -n -E 0 \"$LOCK_FILE\" \"$PY_CMD\" \"$REPO_ROOT/tools/launch_scheduled_pipeline.py\" >> \"$LOG_FILE\" 2>&1")
done

if [[ "${#CRON_LINES[@]}" -eq 0 ]]; then
  echo "PIPELINE_SCHEDULE_TIMES неверный. Пример: 09:00,18:00,21:00" >&2
  exit 1
fi

CRON_BLOCK="$START_MARK"$'\n'"CRON_TZ=$TZ_VALUE"
for line in "${CRON_LINES[@]}"; do
  CRON_BLOCK+=$'\n'"$line"
done
CRON_BLOCK+=$'\n'"$END_MARK"

CURRENT_CRON="$(crontab -l 2>/dev/null || true)"
CLEAN_CRON="$(printf '%s\n' "$CURRENT_CRON" | awk -v s="$START_MARK" -v e="$END_MARK" '
  $0==s {skip=1; next}
  $0==e {skip=0; next}
  !skip {print}
')"

if [[ "${1:-}" == "--remove" ]]; then
  printf '%s\n' "$CLEAN_CRON" | crontab -
  echo "Удалено: cron-задача AutoBotEISPipeline"
  exit 0
fi

if [[ -n "${CLEAN_CRON// }" ]]; then
  NEW_CRON="${CLEAN_CRON}"$'\n'"${CRON_BLOCK}"
else
  NEW_CRON="${CRON_BLOCK}"
fi

printf '%s\n' "$NEW_CRON" | crontab -
echo "Готово: cron-задача установлена (времена: $TIMES_RAW, TZ: $TZ_VALUE)."
echo "Проверка: crontab -l"
echo "Лог: $LOG_FILE"
