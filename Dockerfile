FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends avrdude \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shkaf_stats_bridge.py variables.py templates.py screens.py screens_webui.py settings_webui.py protocol.py ledbar.py qbittorrent.py flash.py flash_webui.py .

EXPOSE 8189

CMD ["python3", "-u", "shkaf_stats_bridge.py"]
