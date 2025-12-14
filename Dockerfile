FROM python:3.11-slim

# Zet werkdirectory
WORKDIR /app

# Kopieer requirements en installeer dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY var/www/assets /app/var/www/assets

# Kopieer alle projectbestanden
COPY . .

# Expose de poort voor FastAPI
EXPOSE 8000

# Start FastAPI via Uvicorn
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
