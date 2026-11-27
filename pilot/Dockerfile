FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 FINDER_PILOT_DB=/data/finder.sqlite3
WORKDIR /srv
COPY requirements.txt /srv/requirements.txt
RUN pip install --no-cache-dir -r /srv/requirements.txt && useradd -r -u 10001 finder && install -d -m 0700 -o finder /data
COPY --chown=finder . /srv/pilot
USER finder
EXPOSE 8080
CMD ["waitress-serve", "--listen=0.0.0.0:8080", "--threads=4", "--call", "pilot.app:create_app"]
