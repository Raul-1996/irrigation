#!/usr/bin/env python3
"""
Специальный тест функционала с фотографиями WB-Irrigation
Детальная проверка загрузки, просмотра и управления фотографиями зон
"""

import unittest
import time
import tempfile
import os
import shutil
import subprocess
import requests
import json
from PIL import Image
import io
import base64

class PhotoFunctionalityTest(unittest.TestCase):
    """Тест функционала с фотографиями"""
    
    @classmethod
    def setUpClass(cls):
        """Настройка перед всеми тестами"""
        # Создаем временные директории
        cls.test_db_path = tempfile.mktemp(suffix='.db')
        cls.test_backup_dir = tempfile.mkdtemp()
        cls.test_photos_dir = tempfile.mkdtemp()
        
        # Настройка сессии requests
        cls.session = requests.Session()
        cls.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
        })
        
        # Запуск Flask приложения в отдельном потоке
        cls.app_process = None
        cls.start_flask_app()
        
        # Ждем запуска приложения
        cls.wait_for_app_startup()
        
        # Создаем тестовые изображения разных размеров
        cls.create_test_images()
    
    @classmethod
    def tearDownClass(cls):
        """Очистка после всех тестов"""
        if cls.app_process:
            cls.app_process.terminate()
            cls.app_process.wait()
        
        # Удаляем временные файлы
        if os.path.exists(cls.test_db_path):
            os.remove(cls.test_db_path)
        if os.path.exists(cls.test_backup_dir):
            shutil.rmtree(cls.test_backup_dir)
        if os.path.exists(cls.test_photos_dir):
            shutil.rmtree(cls.test_photos_dir)
    
    @classmethod
    def start_flask_app(cls):
        """Запуск Flask приложения"""
        env = os.environ.copy()
        env['TESTING'] = '1'
        env['TEST_DB_PATH'] = cls.test_db_path
        env['TEST_BACKUP_DIR'] = cls.test_backup_dir
        env['TEST_PHOTOS_DIR'] = cls.test_photos_dir
        
        cls.app_process = subprocess.Popen(
            ['python', 'run.py'],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
    
    @classmethod
    def wait_for_app_startup(cls):
        """Ожидание запуска приложения"""
        max_attempts = 30
        for attempt in range(max_attempts):
            try:
                response = cls.session.get('http://localhost:8080/api/status', timeout=1)
                if response.status_code == 200:
                    print(f"✅ Приложение запущено на попытке {attempt + 1}")
                    return
            except requests.exceptions.RequestException:
                pass
            time.sleep(1)
        raise Exception("Не удалось запустить приложение")
    
    @classmethod
    def create_test_images(cls):
        """Создание тестовых изображений разных размеров"""
        # Маленькое изображение 100x100
        img_small = Image.new('RGB', (100, 100), color='red')
        img_small_byte_arr = io.BytesIO()
        img_small.save(img_small_byte_arr, format='JPEG')
        img_small_byte_arr.seek(0)
        cls.test_image_small = img_small_byte_arr.getvalue()
        
        # Среднее изображение 500x500
        img_medium = Image.new('RGB', (500, 500), color='blue')
        img_medium_byte_arr = io.BytesIO()
        img_medium.save(img_medium_byte_arr, format='JPEG')
        img_medium_byte_arr.seek(0)
        cls.test_image_medium = img_medium_byte_arr.getvalue()
        
        # Большое изображение 1920x1080
        img_large = Image.new('RGB', (1920, 1080), color='green')
        img_large_byte_arr = io.BytesIO()
        img_large.save(img_large_byte_arr, format='JPEG')
        img_large_byte_arr.seek(0)
        cls.test_image_large = img_large_byte_arr.getvalue()
        
        print("✅ Создано 3 тестовых изображения разных размеров")
    
    def test_01_photo_upload_small_image(self):
        """Тест загрузки маленького изображения"""
        print("🧪 Тест загрузки маленького изображения (100x100)...")
        
        # Создаем временный файл
        temp_file = tempfile.NamedTemporaryFile(suffix='.jpg', delete=False)
        temp_file.write(self.test_image_small)
        temp_file.close()
        
        # Загружаем изображение для зоны 1
        with open(temp_file.name, 'rb') as f:
            files = {'photo': ('test_small.jpg', f, 'image/jpeg')}
            response = self.session.post('http://localhost:8080/api/zones/1/photo', files=files)
        
        # Удаляем временный файл
        os.unlink(temp_file.name)
        
        # Проверяем результат
        self.assertEqual(response.status_code, 200, "Загрузка маленького изображения не удалась")
        result = response.json()
        self.assertIn('success', result)
        self.assertTrue(result['success'])
        print("✅ Маленькое изображение загружено успешно")
    
    def test_02_photo_upload_medium_image(self):
        """Тест загрузки среднего изображения"""
        print("🧪 Тест загрузки среднего изображения (500x500)...")
        
        # Создаем временный файл
        temp_file = tempfile.NamedTemporaryFile(suffix='.jpg', delete=False)
        temp_file.write(self.test_image_medium)
        temp_file.close()
        
        # Загружаем изображение для зоны 2
        with open(temp_file.name, 'rb') as f:
            files = {'photo': ('test_medium.jpg', f, 'image/jpeg')}
            response = self.session.post('http://localhost:8080/api/zones/2/photo', files=files)
        
        # Удаляем временный файл
        os.unlink(temp_file.name)
        
        # Проверяем результат
        self.assertEqual(response.status_code, 200, "Загрузка среднего изображения не удалась")
        result = response.json()
        self.assertIn('success', result)
        self.assertTrue(result['success'])
        print("✅ Среднее изображение загружено успешно")
    
    def test_03_photo_upload_large_image(self):
        """Тест загрузки большого изображения"""
        print("🧪 Тест загрузки большого изображения (1920x1080)...")
        
        # Создаем временный файл
        temp_file = tempfile.NamedTemporaryFile(suffix='.jpg', delete=False)
        temp_file.write(self.test_image_large)
        temp_file.close()
        
        # Загружаем изображение для зоны 3
        with open(temp_file.name, 'rb') as f:
            files = {'photo': ('test_large.jpg', f, 'image/jpeg')}
            response = self.session.post('http://localhost:8080/api/zones/3/photo', files=files)
        
        # Удаляем временный файл
        os.unlink(temp_file.name)
        
        # Проверяем результат
        self.assertEqual(response.status_code, 200, "Загрузка большого изображения не удалась")
        result = response.json()
        self.assertIn('success', result)
        self.assertTrue(result['success'])
        print("✅ Большое изображение загружено успешно")
    
    def test_04_photo_retrieval(self):
        """Тест получения загруженных фотографий"""
        print("🧪 Тест получения загруженных фотографий...")
        
        # Получаем фотографии для всех зон
        for zone_id in [1, 2, 3]:
            response = self.session.get(f'http://localhost:8080/api/zones/{zone_id}/photo?image=true')
            
            if response.status_code == 200:
                # Проверяем, что это изображение
                content_type = response.headers.get('content-type', '')
                self.assertIn('image', content_type, f"Зона {zone_id}: Неверный content-type")
                
                # Проверяем размер изображения
                image_data = response.content
                self.assertGreater(len(image_data), 0, f"Зона {zone_id}: Пустое изображение")
                
                # Проверяем, что это валидное изображение
                try:
                    img = Image.open(io.BytesIO(image_data))
                    print(f"✅ Фотография зоны {zone_id} получена: {img.size[0]}x{img.size[1]} пикселей")
                except Exception as e:
                    self.fail(f"Зона {zone_id}: Невалидное изображение - {e}")
            else:
                print(f"⚠️  Зона {zone_id}: Фотография не найдена (код {response.status_code})")
    
    def test_05_photo_compression(self):
        """Тест сжатия изображений"""
        print("🧪 Тест сжатия изображений...")
        
        # Загружаем большое изображение и проверяем сжатие
        temp_file = tempfile.NamedTemporaryFile(suffix='.jpg', delete=False)
        temp_file.write(self.test_image_large)
        temp_file.close()
        
        # Получаем размер исходного файла
        original_size = len(self.test_image_large)
        
        # Загружаем изображение
        with open(temp_file.name, 'rb') as f:
            files = {'photo': ('test_compression.jpg', f, 'image/jpeg')}
            response = self.session.post('http://localhost:8080/api/zones/4/photo', files=files)
        
        os.unlink(temp_file.name)
        
        self.assertEqual(response.status_code, 200, "Загрузка для теста сжатия не удалась")
        
        # Получаем сжатое изображение
        response = self.session.get('http://localhost:8080/api/zones/4/photo?image=true')
        self.assertEqual(response.status_code, 200, "Получение сжатого изображения не удалось")
        
        compressed_size = len(response.content)
        
        # Проверяем, что изображение было сжато (размер уменьшился)
        self.assertLess(compressed_size, original_size, "Изображение не было сжато")
        
        compression_ratio = (1 - compressed_size / original_size) * 100
        print(f"✅ Сжатие изображения: {compression_ratio:.1f}% (с {original_size} до {compressed_size} байт)")
    
    def test_06_photo_error_handling(self):
        """Тест обработки ошибок при работе с фотографиями"""
        print("🧪 Тест обработки ошибок при работе с фотографиями...")
        
        # Тест 1: Попытка загрузить файл без фотографии
        response = self.session.post('http://localhost:8080/api/zones/1/photo')
        self.assertNotEqual(response.status_code, 200, "Должна быть ошибка при отсутствии файла")
        print("✅ Обработка отсутствующего файла работает корректно")
        
        # Тест 2: Попытка получить фотографию несуществующей зоны
        response = self.session.get('http://localhost:8080/api/zones/999/photo?image=true')
        self.assertEqual(response.status_code, 404, "Должна быть ошибка 404 для несуществующей зоны")
        print("✅ Получение фотографии несуществующей зоны обрабатывается корректно")
        
        # Тест 3: Попытка загрузить файл без фотографии
        response = self.session.post('http://localhost:8080/api/zones/1/photo')
        self.assertNotEqual(response.status_code, 200, "Должна быть ошибка при отсутствии файла")
        print("✅ Обработка отсутствующего файла работает корректно")
    
    def test_07_photo_multiple_formats(self):
        """Тест загрузки изображений разных форматов"""
        print("🧪 Тест загрузки изображений разных форматов...")
        
        # Создаем PNG изображение
        img_png = Image.new('RGB', (200, 200), color='yellow')
        png_byte_arr = io.BytesIO()
        img_png.save(png_byte_arr, format='PNG')
        png_byte_arr.seek(0)
        png_data = png_byte_arr.getvalue()
        
        # Загружаем PNG изображение
        temp_file = temp_file = tempfile.NamedTemporaryFile(suffix='.png', delete=False)
        temp_file.write(png_data)
        temp_file.close()
        
        with open(temp_file.name, 'rb') as f:
            files = {'photo': ('test_png.png', f, 'image/png')}
            response = self.session.post('http://localhost:8080/api/zones/5/photo', files=files)
        
        os.unlink(temp_file.name)
        
        # Проверяем результат
        if response.status_code == 200:
            print("✅ PNG изображение загружено успешно")
        else:
            print(f"⚠️  PNG изображение не загружено (код {response.status_code})")
    
    def test_08_photo_storage_verification(self):
        """Тест проверки хранения фотографий"""
        print("🧪 Тест проверки хранения фотографий...")
        
        # Получаем информацию о зонах
        response = self.session.get('http://localhost:8080/api/zones')
        self.assertEqual(response.status_code, 200, "Не удалось получить список зон")
        
        zones = response.json()
        
        # Проверяем, что у зон есть информация о фотографиях
        zones_with_photos = 0
        for zone in zones:
            if 'photo_path' in zone and zone['photo_path']:
                zones_with_photos += 1
                print(f"✅ Зона {zone['id']}: фотография найдена")
        
        print(f"✅ Всего зон с фотографиями: {zones_with_photos}")
        self.assertGreater(zones_with_photos, 0, "Должна быть хотя бы одна зона с фотографией")
    
    def test_09_photo_performance(self):
        """Тест производительности работы с фотографиями"""
        print("🧪 Тест производительности работы с фотографиями...")
        
        # Тестируем время загрузки фотографии
        start_time = time.time()
        response = self.session.get('http://localhost:8080/api/zones/1/photo?image=true')
        load_time = time.time() - start_time
        
        self.assertEqual(response.status_code, 200, "Не удалось загрузить фотографию")
        self.assertLess(load_time, 1.0, f"Загрузка фотографии слишком медленная: {load_time:.2f} сек")
        print(f"✅ Время загрузки фотографии: {load_time:.3f} сек")
        
        # Тестируем время загрузки нового изображения
        temp_file = tempfile.NamedTemporaryFile(suffix='.jpg', delete=False)
        temp_file.write(self.test_image_small)
        temp_file.close()
        
        start_time = time.time()
        with open(temp_file.name, 'rb') as f:
            files = {'photo': ('test_performance.jpg', f, 'image/jpeg')}
            response = self.session.post('http://localhost:8080/api/zones/6/photo', files=files)
        
        upload_time = time.time() - start_time
        os.unlink(temp_file.name)
        
        self.assertEqual(response.status_code, 200, "Не удалось загрузить изображение")
        self.assertLess(upload_time, 5.0, f"Загрузка изображения слишком медленная: {upload_time:.2f} сек")
        print(f"✅ Время загрузки изображения: {upload_time:.3f} сек")
    
    def test_10_photo_cleanup(self):
        """Тест очистки фотографий"""
        print("🧪 Тест очистки фотографий...")
        
        # Проверяем, что временные файлы не остались
        temp_files = []
        for root, dirs, files in os.walk(self.test_photos_dir):
            for file in files:
                if file.endswith('.tmp'):
                    temp_files.append(os.path.join(root, file))
        
        self.assertEqual(len(temp_files), 0, f"Найдены временные файлы: {temp_files}")
        print("✅ Временные файлы очищены корректно")
        
        # Проверяем, что фотографии сохранились в основной директории
        saved_photos = []
        main_photos_dir = 'static/photos'
        if os.path.exists(main_photos_dir):
            for root, dirs, files in os.walk(main_photos_dir):
                for file in files:
                    if file.endswith(('.jpg', '.jpeg', '.png')):
                        saved_photos.append(os.path.join(root, file))
        
        print(f"✅ Сохранено фотографий: {len(saved_photos)}")
        self.assertGreater(len(saved_photos), 0, "Должны быть сохранены фотографии")

if __name__ == '__main__':
    print("🧪 Запуск специального теста функционала с фотографиями WB-Irrigation...")
    print("=" * 70)
    
    # Запуск тестов
    unittest.main(verbosity=2, exit=False)
    
    print("=" * 70)
    print("🎉 Тест функционала с фотографиями завершен!")
