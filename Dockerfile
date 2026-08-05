FROM python:3.12-slim

WORKDIR /app

# avrdude - для удалённой перепрошивки Pro Micro через тот же USB, без снятия
# платы с сервера (см. будущий /api/firmware/flash). Больше пока ничего не
# делаем с ним в этом шаге - только кладём в образ и чистим apt-кэш.
RUN apt-get update \
    && apt-get install -y --no-install-recommends avrdude \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shkaf_stats_bridge.py variables.py templates.py screens.py screens_webui.py protocol.py ledbar.py qbittorrent.py .

EXPOSE 8189

CMD ["python3", "-u", "shkaf_stats_bridge.py"]
