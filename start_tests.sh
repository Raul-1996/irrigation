#!/bin/bash

# Запуск комплексных тестов WB-Irrigation
# 1) Проверяет доступность веб-сервера (http://127.0.0.1:8080)
# 2) Проверяет доступность MQTT брокера (127.0.0.1:1883)
# 3) Запускает pytest-набор и выводит суммарный результат

set -euo pipefail

ROOT_DIR=$(cd "$(dirname "$0")" && pwd)
cd "$ROOT_DIR"

echo "🚀 Запуск стартера тестов"
echo "================================================"

# Активируем venv
if [ ! -d "venv" ]; then
  echo "❌ Виртуальная среда venv не найдена. Запустите ./setup.sh"
  exit 1
fi
source venv/bin/activate
# Сделаем вывод чище: приглушим лишние предупреждения только для pytest
export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1

# Проверка веб-сервера
BASE_URL="http://127.0.0.1:8080"
echo "🌐 Проверка веб-сервера: ${BASE_URL}"
if command -v curl >/dev/null 2>&1; then
  CODE=$(curl -s -o /dev/null -m 3 -w "%{http_code}" "$BASE_URL/api/status" || true)
else
  CODE=$(python - <<'PY'
import sys, requests
try:
    r = requests.get('http://127.0.0.1:8080/api/status', timeout=3)
    print(r.status_code)
except Exception:
    print('000')
PY
)
fi
if [ "$CODE" != "200" ]; then
  echo "❌ Веб-сервер недоступен (код: $CODE). Запустите приложение (./start.sh) и повторите."
  exit 1
fi
echo "✅ Веб-сервер доступен"

# Дополнительно: health-check с порогом времени ответа
echo "🩺 Проверка /health"
START_TS=$(python3 - <<'PY'
import time; print(int(time.time()*1000))
PY
)
HC_CODE=$(curl -s -o /dev/null -m 3 -w "%{http_code}" "$BASE_URL/health" || true)
END_TS=$(python3 - <<'PY'
import time; print(int(time.time()*1000))
PY
)
ELAPSED=$((END_TS-START_TS))
if [ "$HC_CODE" != "200" ]; then
  echo "❌ Health check не прошёл (код: $HC_CODE)"
  exit 1
fi
THRESHOLD=1500
if [ $ELAPSED -gt $THRESHOLD ]; then
  echo "⚠️  Health check медленный: ${ELAPSED}ms (> ${THRESHOLD}ms)"
else
  echo "✅ Health OK за ${ELAPSED}ms"
fi

# Проверка MQTT брокера
MQTT_HOST="127.0.0.1"
MQTT_PORT=1883
echo "📡 Проверка MQTT брокера: ${MQTT_HOST}:${MQTT_PORT}"
python - <<PY
import sys
try:
    import paho.mqtt.client as mqtt
    cl = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    cl.connect('${MQTT_HOST}', ${MQTT_PORT}, 3)
    cl.disconnect()
    print('OK')
    sys.exit(0)
except Exception as e:
    print('ERR', e)
    sys.exit(2)
PY
MQTT_RC=$?
if [ $MQTT_RC -ne 0 ]; then
  echo "❌ MQTT брокер недоступен. Убедитесь, что он запущен (например, 127.0.0.1:1883)."
  exit 1
fi
echo "✅ MQTT брокер доступен"

echo "================================================"
echo "🧪 Запуск pytest-набора (unit/API)"
set +e
pytest -q -r a \
  -W ignore::DeprecationWarning \
  -W ignore::urllib3.exceptions.NotOpenSSLWarning \
  -W ignore::pytest.PytestUnknownMarkWarning
PYTEST_RC=$?
set -e

echo "================================================"
if [ $PYTEST_RC -eq 0 ]; then
  echo "🎉 Все тесты пройдены успешно"
  exit 0
else
  echo "⚠️  Обнаружены ошибки: pytest_rc=$PYTEST_RC"
  exit 1
fi

