FROM python:3.12-slim

WORKDIR /app

# Системные зависимости для Playwright
RUN apt-get update && apt-get install -y \
    curl wget gnupg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Браузер для Playwright (Chromium)
RUN playwright install chromium --with-deps

COPY . .

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
