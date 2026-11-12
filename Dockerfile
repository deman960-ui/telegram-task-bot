FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# постоянный том bothost; resolve_db_path() создаст подкаталог сам,
# но пустая папка на всякий случай должна существовать
RUN mkdir -p /app/data

CMD ["python", "main.py"]
