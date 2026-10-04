FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN apt-get update \
    && apt-get install -y --no-install-recommends p7zip-full \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium
COPY . .
RUN useradd --create-home --uid 10001 botuser && mkdir -p /app/data && chown -R botuser:botuser /app
USER botuser
EXPOSE 10000
CMD ["python", "bot.py"]
