FROM mcr.microsoft.com/playwright/python:v1.61.0-noble

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WEB_UI_HOST=0.0.0.0 \
    WEB_UI_PORT=8765 \
    MARKET_AVITO_BROWSER=1 \
    MARKET_AVITO_HEADLESS=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends p7zip-full p7zip-rar tesseract-ocr tesseract-ocr-rus \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt pyproject.toml README.md ./
RUN grep -v -E '^playwright[[:space:]]*$' requirements.txt > /tmp/requirements-docker.txt \
    && pip install --no-cache-dir -r /tmp/requirements-docker.txt \
    && pip install --no-cache-dir playwright==1.61.0

COPY autobot ./autobot
COPY tools ./tools
COPY botctl.py ./

RUN mkdir -p /app/data

EXPOSE 8765

CMD ["python", "tools/launch_web_ui.py"]
