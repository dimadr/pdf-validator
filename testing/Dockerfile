ARG PROJECT1_PYTHON_IMAGE=python:3.11-slim-bookworm
FROM ${PROJECT1_PYTHON_IMAGE}
ARG PROJECT1_ACCEPTANCE_RUN_ID
LABEL io.project1.acceptance.run="${PROJECT1_ACCEPTANCE_RUN_ID}"
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app
WORKDIR /app
COPY constraints.txt /tmp/constraints.txt
RUN apt-get update && apt-get install -y --no-install-recommends postgresql-client fonts-dejavu-core poppler-utils && rm -rf /var/lib/apt/lists/*
COPY backend/requirements.txt /tmp/backend-requirements.txt
COPY worker/requirements.txt /tmp/worker-requirements.txt
COPY testing/requirements.txt /tmp/testing-requirements.txt
RUN pip install --no-cache-dir -c /tmp/constraints.txt -r /tmp/backend-requirements.txt -r /tmp/worker-requirements.txt -r /tmp/testing-requirements.txt
COPY common /app/common
COPY backend /app/backend
COPY worker /app/worker
COPY tests /app/tests
COPY tools /app/tools
COPY testing /app/testing
COPY ui /app/ui
COPY deploy /app/deploy
CMD ["python", "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider"]
