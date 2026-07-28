FROM mcr.microsoft.com/playwright/python:v1.45.0-jammy

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WEB_UI_HOST=0.0.0.0 \
    WEB_UI_PORT=8765 \
    MARKET_AVITO_HEADLESS=1

WORKDIR /app

COPY requirements.txt pyproject.toml README.md ./
RUN grep -v -E '^playwright[[:space:]]*$' requirements.txt > /tmp/requirements-docker.txt \
    && pip install --no-cache-dir -r /tmp/requirements-docker.txt \
    && pip install --no-cache-dir playwright==1.45.0

COPY autobot ./autobot
COPY tools ./tools
COPY botctl.py ./

RUN mkdir -p /app/data

EXPOSE 8765

CMD ["python", "tools/launch_web_ui.py"]
