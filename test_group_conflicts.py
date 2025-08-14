#!/usr/bin/env python3
"""
Тест проверки конфликтов программ по группам
Проверяет, что система корректно обнаруживает конфликты между программами,
которые используют зоны из одной группы в пересекающееся время
"""

import sys
import os
import json
import tempfile
import shutil
from datetime import datetime

# Добавляем путь к проекту
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from database import IrrigationDB

def test_group_conflicts():
    """Тест проверки конфликтов по группам"""
    print("🧪 Тест проверки конфликтов программ по группам")
    print("=" * 60)
    
    # Создаем временную базу данных
    temp_dir = tempfile.mkdtemp()
    db_path = os.path.join(temp_dir, 'test_irrigation.db')
    
    try:
        # Инициализируем базу данных
        db = IrrigationDB(db_path)
        db.init_database()
        
        # Создаем тестовые данные
        print("📝 Создание тестовых данных...")
        
        # Используем существующую группу (группа 1 - Газон)
        group_id = 1
        print(f"✅ Используем существующую группу: Газон (ID: {group_id})")
        
        # Используем существующие зоны из группы 1
        zones = []
        for i in range(1, 4):  # Зоны 1, 2, 3
            zone = db.get_zone(i)
            if zone:
                zones.append(zone)
                print(f"✅ Используем зону: {zone['name']} (ID: {zone['id']})")
            else:
                print(f"❌ Зона {i} не найдена")
                return False
        
        # Создаем первую программу (зоны 1 и 2)
        program1_data = {
            'name': 'Программа 1',
            'time': '08:00',
            'days': ['Пн', 'Вт', 'Ср'],
            'zones': [zones[0]['id'], zones[1]['id']]  # Зоны 1 и 2
        }
        program1 = db.create_program(program1_data)
        print(f"✅ Создана программа 1: {program1['name']} (ID: {program1['id']})")
        print(f"   Время: {program1['time']}, Зоны: {program1['zones']}")
        
        # Создаем вторую программу (зоны 2 и 3) - пересечение по зоне 2
        program2_data = {
            'name': 'Программа 2',
            'time': '08:05',  # Начинается через 5 минут после первой
            'days': ['Пн', 'Вт', 'Ср'],
            'zones': [zones[1]['id'], zones[2]['id']]  # Зоны 2 и 3
        }
        program2 = db.create_program(program2_data)
        print(f"✅ Создана программа 2: {program2['name']} (ID: {program2['id']})")
        print(f"   Время: {program2['time']}, Зоны: {program2['zones']}")
        
        # Создаем третью программу (зона 1) - пересечение по группе
        program3_data = {
            'name': 'Программа 3',
            'time': '08:10',  # Начинается через 10 минут после первой
            'days': ['Пн', 'Вт', 'Ср'],
            'zones': [zones[0]['id']]  # Только зона 1
        }
        program3 = db.create_program(program3_data)
        print(f"✅ Создана программа 3: {program3['name']} (ID: {program3['id']})")
        print(f"   Время: {program3['time']}, Зоны: {program3['zones']}")
        
        print("\n🔍 Тестирование проверки конфликтов...")
        
        # Тест 1: Проверка конфликта по пересекающимся зонам
        print("\n📋 Тест 1: Конфликт по пересекающимся зонам")
        conflicts1 = db.check_program_conflicts(
            program_id=program1['id'],
            time='08:00',
            zones=[zones[0]['id'], zones[1]['id']],
            days=['Пн', 'Вт', 'Ср']
        )
        
        if conflicts1:
            print("✅ Конфликт обнаружен (ожидаемо)")
            for conflict in conflicts1:
                print(f"   - Программа {conflict['program_name']} (ID: {conflict['program_id']})")
                print(f"     Пересекающиеся зоны: {conflict['common_zones']}")
                print(f"     Пересекающиеся группы: {conflict['common_groups']}")
        else:
            print("❌ Конфликт не обнаружен (неожиданно)")
        
        # Тест 2: Проверка конфликта по группе (без пересечения зон)
        print("\n📋 Тест 2: Конфликт по группе (без пересечения зон)")
        conflicts2 = db.check_program_conflicts(
            program_id=program1['id'],
            time='08:00',
            zones=[zones[0]['id']],  # Только зона 1
            days=['Пн', 'Вт', 'Ср']
        )
        
        if conflicts2:
            print("✅ Конфликт по группе обнаружен (ожидаемо)")
            for conflict in conflicts2:
                print(f"   - Программа {conflict['program_name']} (ID: {conflict['program_id']})")
                print(f"     Пересекающиеся зоны: {conflict['common_zones']}")
                print(f"     Пересекающиеся группы: {conflict['common_groups']}")
        else:
            print("❌ Конфликт по группе не обнаружен (неожиданно)")
        
        # Тест 3: Проверка отсутствия конфликта при разных днях
        print("\n📋 Тест 3: Отсутствие конфликта при разных днях")
        conflicts3 = db.check_program_conflicts(
            program_id=program1['id'],
            time='08:00',
            zones=[zones[0]['id'], zones[1]['id']],
            days=['Чт', 'Пт']  # Другие дни
        )
        
        if not conflicts3:
            print("✅ Конфликт отсутствует (ожидаемо) - разные дни")
        else:
            print("❌ Конфликт обнаружен (неожиданно) - разные дни")
        
        # Тест 4: Проверка отсутствия конфликта при разном времени
        print("\n📋 Тест 4: Отсутствие конфликта при разном времени")
        conflicts4 = db.check_program_conflicts(
            program_id=program1['id'],
            time='10:00',  # Другое время
            zones=[zones[0]['id'], zones[1]['id']],
            days=['Пн', 'Вт', 'Ср']
        )
        
        if not conflicts4:
            print("✅ Конфликт отсутствует (ожидаемо) - разное время")
        else:
            print("❌ Конфликт обнаружен (неожиданно) - разное время")
        
        print("\n" + "=" * 60)
        print("🎉 Тест проверки конфликтов по группам завершен!")
        
        return True
        
    except Exception as e:
        print(f"❌ Ошибка в тесте: {e}")
        return False
        
    finally:
        # Очищаем временные файлы
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)

if __name__ == '__main__':
    success = test_group_conflicts()
    sys.exit(0 if success else 1)
