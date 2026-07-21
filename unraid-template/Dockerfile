FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shkaf_stats_bridge.py .

CMD ["python3", "-u", "shkaf_stats_bridge.py"]
