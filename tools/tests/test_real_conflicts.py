#!/usr/bin/env python3
"""
Тест реального сценария конфликтов программ
Проверяет конкретный случай из скриншотов пользователя:
- Программа 4: время 05:00, зоны 1-22, 24-30
- Программа 5: время 09:00, зоны 1, 2, 3, 4
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

def test_real_conflicts():
    """Тест реального сценария конфликтов"""
    print("🧪 Тест реального сценария конфликтов программ")
    print("=" * 60)
    
    # Создаем временную базу данных
    temp_dir = tempfile.mkdtemp()
    db_path = os.path.join(temp_dir, 'test_irrigation.db')
    
    try:
        # Инициализируем базу данных
        db = IrrigationDB(db_path)
        db.init_database()
        
        print("📝 Создание тестовых программ...")
        
        # Создаем программу 4 (как в скриншоте)
        program4_data = {
            'name': 'test',
            'time': '05:00',
            'days': ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'],
            'zones': [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 24, 25, 26, 27, 28, 29, 30]
        }
        program4 = db.create_program(program4_data)
        print(f"✅ Создана программа 4: {program4['name']} (ID: {program4['id']})")
        print(f"   Время: {program4['time']}, Зоны: {program4['zones']}")
        
        # Создаем программу 5 (как в скриншоте)
        program5_data = {
            'name': 'проверка',
            'time': '09:00',
            'days': ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'],
            'zones': [1, 2, 3, 4]
        }
        program5 = db.create_program(program5_data)
        print(f"✅ Создана программа 5: {program5['name']} (ID: {program5['id']})")
        print(f"   Время: {program5['time']}, Зоны: {program5['zones']}")
        
        print("\n🔍 Тестирование проверки конфликтов...")
        
        # Тест 1: Проверяем конфликт при создании программы 5
        print("\n📋 Тест 1: Конфликт при создании программы 5")
        conflicts1 = db.check_program_conflicts(
            program_id=program5['id'],
            time='09:00',
            zones=[1, 2, 3, 4],
            days=['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']
        )
        
        print(f"Найдено конфликтов: {len(conflicts1)}")
        for i, conflict in enumerate(conflicts1, 1):
            print(f"   Конфликт {i}:")
            print(f"     - Программа: {conflict['program_name']} (ID: {conflict['program_id']})")
            print(f"     - Время: {conflict['program_time']}")
            print(f"     - Пересекающиеся зоны: {conflict['common_zones']}")
            print(f"     - Пересекающиеся группы: {conflict['common_groups']}")
            print(f"     - Пересечение времени: {conflict['overlap_start']} - {conflict['overlap_end']}")
        
        # Тест 2: Проверяем конфликт при создании программы 4
        print("\n📋 Тест 2: Конфликт при создании программы 4")
        conflicts2 = db.check_program_conflicts(
            program_id=program4['id'],
            time='05:00',
            zones=[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 24, 25, 26, 27, 28, 29, 30],
            days=['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']
        )
        
        print(f"Найдено конфликтов: {len(conflicts2)}")
        for i, conflict in enumerate(conflicts2, 1):
            print(f"   Конфликт {i}:")
            print(f"     - Программа: {conflict['program_name']} (ID: {conflict['program_id']})")
            print(f"     - Время: {conflict['program_time']}")
            print(f"     - Пересекающиеся зоны: {conflict['common_zones']}")
            print(f"     - Пересекающиеся группы: {conflict['common_groups']}")
            print(f"     - Пересечение времени: {conflict['overlap_start']} - {conflict['overlap_end']}")
        
        # Тест 3: Проверяем длительность программ
        print("\n📋 Тест 3: Расчет длительности программ")
        
        # Длительность программы 4 (суммарная из зон 1-22, 24-30)
        total_duration_4 = 0
        for zone_id in program4['zones']:
            duration = db.get_zone_duration(zone_id)
            total_duration_4 += duration
            print(f"   Зона {zone_id}: {duration} мин")
        print(f"   Суммарная длительность программы 4: {total_duration_4} мин")
        
        # Длительность программы 5 (суммарная из зон 1-4)
        total_duration_5 = 0
        for zone_id in program5['zones']:
            duration = db.get_zone_duration(zone_id)
            total_duration_5 += duration
            print(f"   Зона {zone_id}: {duration} мин")
        print(f"   Суммарная длительность программы 5: {total_duration_5} мин")
        
        # Проверяем, пересекаются ли программы по времени
        start_4 = 5 * 60  # 05:00 в минутах
        end_4 = start_4 + total_duration_4
        start_5 = 9 * 60  # 09:00 в минутах
        end_5 = start_5 + total_duration_5
        
        print(f"\n   Время программы 4: {start_4//60:02d}:{start_4%60:02d} - {end_4//60:02d}:{end_4%60:02d}")
        print(f"   Время программы 5: {start_5//60:02d}:{start_5%60:02d} - {end_5//60:02d}:{end_5%60:02d}")
        
        # Проверяем пересечение
        overlap_start = max(start_4, start_5)
        overlap_end = min(end_4, end_5)
        
        if overlap_start < overlap_end:
            print(f"   ⚠️  ПРОГРАММЫ ПЕРЕСЕКАЮТСЯ!")
            print(f"   Пересечение: {overlap_start//60:02d}:{overlap_start%60:02d} - {overlap_end//60:02d}:{overlap_end%60:02d}")
        else:
            print(f"   ✅ Программы НЕ пересекаются по времени")
        
        print("\n" + "=" * 60)
        print("🎉 Тест реального сценария завершен!")
        
        return True
        
    except Exception as e:
        print(f"❌ Ошибка в тесте: {e}")
        import traceback
        traceback.print_exc()
        return False
        
    finally:
        # Очищаем временные файлы
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)

if __name__ == '__main__':
    success = test_real_conflicts()
    sys.exit(0 if success else 1)
