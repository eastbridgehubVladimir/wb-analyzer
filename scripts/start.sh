#!/bin/bash
# Быстрый запуск всего стека локально

set -e

cd "$(dirname "$0")/.."

# Копировать .env если ещё нет
if [ ! -f .env ]; then
  cp .env.example .env
  echo "Создан .env — заполните переменные WB_API_KEY и PROXY_LIST"
fi

echo "Запускаем PostgreSQL, ClickHouse, Redis..."
docker compose up -d postgres clickhouse redis

echo "Ждём готовности баз данных..."
sleep 5

echo "Запускаем API и воркер..."
docker compose up -d api worker

echo ""
echo "Готово!"
echo "  API:  http://localhost:8000"
echo "  Docs: http://localhost:8000/docs"
echo "  ClickHouse HTTP: http://localhost:8123"
