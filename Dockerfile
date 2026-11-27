FROM python:3.11-slim

# --- build args / defaults for the runtime user ---
ARG APP_UID=10001
ARG APP_GID=10001
ARG APP_USER=appuser

WORKDIR /app

# This deployment monitors Vinted only. Preserve historical eBay data and redactions.
ENV FINDER_EBAY_ALERTS=0

# System deps: gosu to drop privileges cleanly
RUN apt-get update \
 && apt-get install -y --no-install-recommends gosu fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

# Create runtime user/group and app dirs (inside image)
RUN groupadd -g ${APP_GID} ${APP_USER} \
 && useradd -u ${APP_UID} -g ${APP_GID} -M ${APP_USER} \
 && mkdir -p /app/data /app/logs

# Copy requirements first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The private reconnect browser is launched only by an authenticated owner.
# Keep its runtime available to the non-root app user; no always-on browser.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/msj-browsers
RUN python -m playwright install --with-deps chromium \
 && chmod -R a+rX /opt/msj-browsers \
 && rm -rf /var/lib/apt/lists/*

# The pinned image encoder is loaded only by short-lived Home photo checks.
COPY style_assets/requirements.txt /tmp/home-style-requirements.txt
RUN pip install --no-cache-dir -r /tmp/home-style-requirements.txt
COPY style_assets/install_model.py /tmp/install-home-style-model.py
RUN python /tmp/install-home-style-model.py

# Copy the rest of the application
COPY . .

# EntryPoint script (added below)
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Expose ports
EXPOSE 8000
EXPOSE 8080

# Run as root for the entrypoint to adjust ownership if needed,
# then the entrypoint will drop to ${APP_USER}.
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "vinted_notifications.py"]
