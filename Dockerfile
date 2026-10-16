FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
# Chromium prints the site briefs to PDF (crm/land/briefs.py html_to_pdf).
RUN apt-get update && apt-get install -y --no-install-recommends chromium fonts-liberation \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
# Static files are baked into the image. The key is a build-time placeholder only (settings insist on 50+ characters
# when DEBUG is off); the real one comes from Secret Manager at run time.
RUN DJANGO_DEBUG=false DJANGO_SECRET_KEY=build-only-placeholder-never-used-at-runtime-0123456789 \
    python manage.py collectstatic --noinput
# Run as an ordinary user, not root. Cloud Run gives Chrome no user namespaces for its own sandbox, so it is started
# with --no-sandbox (ACMI_CHROME_NO_SANDBOX); it only prints the app's own autoescaped pages.
RUN useradd --create-home --uid 1000 app && mkdir -p /app/media && chown -R app /app/media
USER app
ENV DJANGO_DEBUG=false PORT=8080 ACMI_CHROME_NO_SANDBOX=1
# Two workers with threads. Nothing is shared in process memory: the mail sync's lock and the send batches live in
# the database.
CMD exec gunicorn config.wsgi:application --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 600
