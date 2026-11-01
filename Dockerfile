FROM python:3.13-alpine
RUN apk add --no-cache poppler-utils \
    && adduser -D -u 1000 dashboard \
    && install -d -o dashboard /data
COPY print_dashboard /app/print_dashboard
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER dashboard
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s \
    CMD python3 -c "import os, urllib.request as r; r.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8080') + '/healthz', timeout=3)"
CMD ["python3", "-m", "print_dashboard"]
