# Используем легкий образ Python
FROM python:3.11-slim

# Устанавливаем рабочую директорию
WORKDIR /app

# Устанавливаем системные зависимости для работы с изображениями
RUN apt-get update && apt-get install -y \
    gcc \
    libjpeg-dev \
    zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

# Копируем требования и устанавливаем их
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Chromium для Playwright (рендер сайтов под JS-challenge Cloudflare).
# --with-deps доустанавливает системные библиотеки, которых нет в slim-образе.
RUN playwright install --with-deps chromium

# Копируем код проекта
COPY . .

# Создаем папку для картинок
RUN mkdir -p gen_images

# Запускаем бота
CMD ["python", "main.py"]
