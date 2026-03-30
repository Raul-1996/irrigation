#!/bin/bash
# Programs v2 Tests Runner
# Запускает все тесты для нового функционала programs v2

set -e

echo "========================================="
echo "Programs v2 Tests (TDD)"
echo "========================================="
echo ""

# Цвета для вывода
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Проверка что мы в правильной директории
if [ ! -f "app.py" ]; then
    echo -e "${RED}Ошибка: запустите скрипт из корня проекта wb-irrigation${NC}"
    exit 1
fi

# Активация виртуальной среды если есть
if [ -d "venv" ]; then
    echo -e "${YELLOW}Активация виртуальной среды...${NC}"
    source venv/bin/activate
fi

# Проверка pytest
if ! command -v pytest &> /dev/null; then
    echo -e "${RED}pytest не найден. Установите: pip install pytest pytest-timeout${NC}"
    exit 1
fi

echo -e "${GREEN}pytest найден: $(which pytest)${NC}"
echo ""

# Функция для запуска тестов
run_tests() {
    local test_file=$1
    local test_name=$2
    
    echo -e "${YELLOW}=========================================${NC}"
    echo -e "${YELLOW}Запуск: ${test_name}${NC}"
    echo -e "${YELLOW}=========================================${NC}"
    
    if pytest "$test_file" -v --tb=short; then
        echo -e "${GREEN}✓ ${test_name}: PASS${NC}"
        echo ""
        return 0
    else
        echo -e "${RED}✗ ${test_name}: FAIL${NC}"
        echo ""
        return 1
    fi
}

# Счётчики
TOTAL=0
PASSED=0
FAILED=0

# 1. Тесты БД
if run_tests "tests/db/test_programs_db_v2.py" "DB Tests (33 tests)"; then
    PASSED=$((PASSED + 1))
else
    FAILED=$((FAILED + 1))
fi
TOTAL=$((TOTAL + 1))

# 2. Тесты API
if run_tests "tests/api/test_programs_api_v2.py" "API Tests (31 tests)"; then
    PASSED=$((PASSED + 1))
else
    FAILED=$((FAILED + 1))
fi
TOTAL=$((TOTAL + 1))

# 3. Тесты Scheduler
if run_tests "tests/unit/test_scheduler_v2.py" "Scheduler Tests (27 tests)"; then
    PASSED=$((PASSED + 1))
else
    FAILED=$((FAILED + 1))
fi
TOTAL=$((TOTAL + 1))

# Итого
echo ""
echo -e "${YELLOW}=========================================${NC}"
echo -e "${YELLOW}ИТОГО${NC}"
echo -e "${YELLOW}=========================================${NC}"
echo -e "Всего наборов тестов: $TOTAL"
echo -e "${GREEN}Пройдено: $PASSED${NC}"
echo -e "${RED}Провалено: $FAILED${NC}"
echo ""

if [ $FAILED -eq 0 ]; then
    echo -e "${GREEN}✓ Все тесты пройдены успешно!${NC}"
    echo ""
    echo "Можно приступать к следующему этапу реализации."
    exit 0
else
    echo -e "${RED}✗ Некоторые тесты провалены.${NC}"
    echo ""
    echo "Это нормально если функционал ещё не реализован (xfail)."
    echo "После реализации убирайте @pytest.mark.xfail и перезапускайте."
    echo ""
    echo "Подробности:"
    echo "  - tests/TESTS_V2_README.md — документация"
    echo "  - TESTS_V2_SUMMARY.md — краткая сводка"
    exit 1
fi
