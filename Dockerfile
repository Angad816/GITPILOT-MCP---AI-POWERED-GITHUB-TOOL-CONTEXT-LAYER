FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    GITPILOT_HOST=0.0.0.0 \
    GITPILOT_PORT=8765

WORKDIR /app

RUN groupadd --system gitpilot && useradd --system --gid gitpilot gitpilot && mkdir -p /app/data /workspaces && chown -R gitpilot:gitpilot /app/data /workspaces
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY dashboard.py server.py ./
COPY services ./services
COPY web ./web

USER gitpilot
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=4)"

CMD ["python", "dashboard.py"]
