FROM python:3.11-alpine

WORKDIR /app
COPY monitor.py healthcheck.py /app/
RUN mkdir -p /data

VOLUME ["/data"]

HEALTHCHECK --interval=60s --timeout=15s --start-period=120s --retries=3 \
  CMD python /app/healthcheck.py

CMD ["python", "-u", "/app/monitor.py"]
