#!/usr/bin/env python3
"""
Система планировщика полива WB-Irrigation
Реализует алгоритм последовательного запуска зон по принципам Hunter
"""

import schedule
import time
import threading
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
import logging
from database import IrrigationDB
import json

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class IrrigationScheduler:
    """Планировщик полива с последовательным запуском зон"""
    
    def __init__(self, db: IrrigationDB):
        self.db = db
        self.running_programs = {}  # ID программы -> статус
        self.active_zones = {}      # ID зоны -> время окончания
        self.scheduler_thread = None
        self.is_running = False
        
    def start(self):
        """Запуск планировщика"""
        if self.is_running:
            return
            
        self.is_running = True
        self.scheduler_thread = threading.Thread(target=self._scheduler_loop, daemon=True)
        self.scheduler_thread.start()
        logger.info("Планировщик полива запущен")
        
    def stop(self):
        """Остановка планировщика"""
        self.is_running = False
        if self.scheduler_thread:
            self.scheduler_thread.join()
        logger.info("Планировщик полива остановлен")
        
    def _scheduler_loop(self):
        """Основной цикл планировщика"""
        while self.is_running:
            try:
                schedule.run_pending()
                time.sleep(1)
                
                # Проверяем завершение активных зон
                self._check_zone_completion()
                
            except Exception as e:
                logger.error(f"Ошибка в планировщике: {e}")
                time.sleep(5)
    
    def _check_zone_completion(self):
        """Проверка завершения работы зон"""
        current_time = datetime.now()
        completed_zones = []
        
        for zone_id, end_time in self.active_zones.items():
            if current_time >= end_time:
                completed_zones.append(zone_id)
                self._stop_zone(zone_id)
        
        # Удаляем завершенные зоны
        for zone_id in completed_zones:
            del self.active_zones[zone_id]
    
    def _stop_zone(self, zone_id: int):
        """Остановка зоны полива"""
        try:
            # Обновляем состояние зоны в базе данных
            self.db.update_zone(zone_id, {'state': 'off'})
            
            # Логируем остановку
            zone = self.db.get_zone(zone_id)
            if zone:
                self.db.add_log('zone_auto_stop', f'Зона {zone_id} ({zone["name"]}) автоматически остановлена')
            
            logger.info(f"Зона {zone_id} остановлена")
            
        except Exception as e:
            logger.error(f"Ошибка остановки зоны {zone_id}: {e}")
    
    def schedule_program(self, program_id: int, program_data: Dict[str, Any]):
        """Планирование программы полива"""
        try:
            # Парсим время запуска
            start_time = datetime.strptime(program_data['time'], '%H:%M').time()
            
            # Получаем дни недели
            days = program_data['days']
            if not days:
                logger.warning(f"Программа {program_id} не имеет дней недели")
                return
            
            # Получаем зоны программы
            zones = program_data['zones']
            if not zones:
                logger.warning(f"Программа {program_id} не имеет зон")
                return
            
            # Сортируем зоны по ID (от меньшего к большему)
            zones.sort()
            
            # Планируем запуск для каждого дня
            for day in days:
                day_name = self._get_day_name(day)
                if day_name:
                    schedule.every().day.at(program_data['time']).do(
                        self._run_program, program_id, zones, program_data['name']
                    ).tag(f"program_{program_id}_{day}")
            
            self.running_programs[program_id] = {
                'name': program_data['name'],
                'zones': zones,
                'days': days,
                'time': program_data['time']
            }
            
            logger.info(f"Программа {program_id} ({program_data['name']}) запланирована")
            
        except Exception as e:
            logger.error(f"Ошибка планирования программы {program_id}: {e}")
    
    def _get_day_name(self, day: str) -> Optional[str]:
        """Преобразование дня недели в название"""
        day_mapping = {
            'monday': 'monday',
            'tuesday': 'tuesday', 
            'wednesday': 'wednesday',
            'thursday': 'thursday',
            'friday': 'friday',
            'saturday': 'saturday',
            'sunday': 'sunday'
        }
        return day_mapping.get(day.lower())
    
    def _run_program(self, program_id: int, zones: List[int], program_name: str):
        """Запуск программы полива"""
        try:
            logger.info(f"Запуск программы {program_id} ({program_name})")
            
            # Проверяем, не запущена ли уже программа
            if program_id in self.running_programs:
                logger.warning(f"Программа {program_id} уже запущена")
                return
            
            # Запускаем зоны последовательно
            self._run_zones_sequentially(zones, program_id, program_name)
            
        except Exception as e:
            logger.error(f"Ошибка запуска программы {program_id}: {e}")
    
    def _run_zones_sequentially(self, zones: List[int], program_id: int, program_name: str):
        """Последовательный запуск зон"""
        try:
            current_time = datetime.now()
            
            for i, zone_id in enumerate(zones):
                # Проверяем, не отложен ли полив зоны
                zone = self.db.get_zone(zone_id)
                if not zone:
                    logger.warning(f"Зона {zone_id} не найдена")
                    continue
                
                if zone.get('postpone_until'):
                    postpone_until = datetime.strptime(zone['postpone_until'], '%Y-%m-%d %H:%M')
                    if current_time < postpone_until:
                        logger.info(f"Зона {zone_id} отложена до {zone['postpone_until']}")
                        continue
                    else:
                        # Снимаем отложенный полив
                        self.db.update_zone_postpone(zone_id, None)
                
                # Запускаем зону
                self._start_zone(zone_id, zone['duration'], program_id, program_name)
                
                # Ждем завершения зоны перед запуском следующей
                if i < len(zones) - 1:  # Не ждем после последней зоны
                    time.sleep(zone['duration'] * 60)  # Конвертируем минуты в секунды
            
            logger.info(f"Программа {program_id} ({program_name}) завершена")
            
        except Exception as e:
            logger.error(f"Ошибка последовательного запуска зон: {e}")
    
    def _start_zone(self, zone_id: int, duration: int, program_id: int, program_name: str):
        """Запуск отдельной зоны"""
        try:
            # Обновляем состояние зоны
            self.db.update_zone(zone_id, {'state': 'on'})
            
            # Вычисляем время окончания
            end_time = datetime.now() + timedelta(minutes=duration)
            self.active_zones[zone_id] = end_time
            
            # Логируем запуск
            zone = self.db.get_zone(zone_id)
            if zone:
                self.db.add_log('zone_auto_start', json.dumps({
                    'zone_id': zone_id,
                    'zone_name': zone['name'],
                    'program_id': program_id,
                    'program_name': program_name,
                    'duration': duration,
                    'end_time': end_time.strftime('%Y-%m-%d %H:%M:%S')
                }))
            
            logger.info(f"Зона {zone_id} запущена на {duration} минут")
            
        except Exception as e:
            logger.error(f"Ошибка запуска зоны {zone_id}: {e}")
    
    def cancel_program(self, program_id: int):
        """Отмена программы полива"""
        try:
            # Удаляем все задачи программы
            schedule.clear(f"program_{program_id}")
            
            # Удаляем из активных программ
            if program_id in self.running_programs:
                del self.running_programs[program_id]
            
            logger.info(f"Программа {program_id} отменена")
            
        except Exception as e:
            logger.error(f"Ошибка отмены программы {program_id}: {e}")
    
    def get_active_programs(self) -> Dict[int, Dict[str, Any]]:
        """Получение активных программ"""
        return self.running_programs.copy()
    
    def get_active_zones(self) -> Dict[int, datetime]:
        """Получение активных зон"""
        return self.active_zones.copy()
    
    def load_programs(self):
        """Загрузка всех программ из базы данных"""
        try:
            programs = self.db.get_programs()
            
            for program in programs:
                self.schedule_program(program['id'], program)
            
            logger.info(f"Загружено {len(programs)} программ")
            
        except Exception as e:
            logger.error(f"Ошибка загрузки программ: {e}")

# Глобальный экземпляр планировщика
scheduler = None

def init_scheduler(db: IrrigationDB):
    """Инициализация планировщика"""
    global scheduler
    scheduler = IrrigationScheduler(db)
    scheduler.start()
    scheduler.load_programs()
    return scheduler

def get_scheduler() -> Optional[IrrigationScheduler]:
    """Получение экземпляра планировщика"""
    return scheduler
