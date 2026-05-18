FROM python:3.11-slim

# Installa TOR
RUN apt-get update && apt-get install -y tor && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Crea cartella dati TOR
RUN mkdir -p /tmp/tor_data

EXPOSE 5000

CMD gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120
