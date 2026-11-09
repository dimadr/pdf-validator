FROM python:3.11-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app:/app/worker
WORKDIR /app
COPY constraints.txt /tmp/constraints.txt
RUN apt-get update && apt-get install -y --no-install-recommends gcc libpq-dev && rm -rf /var/lib/apt/lists/*
COPY worker/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -c /tmp/constraints.txt -r /app/requirements.txt
COPY common /app/common
COPY worker /app/worker
CMD ["celery", "-A", "celery_app", "worker", "--loglevel=info"]
