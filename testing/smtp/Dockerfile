ARG PROJECT1_PYTHON_IMAGE=python:3.11-slim-bookworm
FROM ${PROJECT1_PYTHON_IMAGE}
ARG PROJECT1_ACCEPTANCE_RUN_ID
LABEL io.project1.acceptance.run="${PROJECT1_ACCEPTANCE_RUN_ID}"
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN pip install --no-cache-dir aiosmtpd==1.4.6
WORKDIR /app
COPY testing/smtp/sink.py /app/sink.py
EXPOSE 1025 8025
CMD ["python", "/app/sink.py"]
