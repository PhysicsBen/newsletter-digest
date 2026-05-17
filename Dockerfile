FROM python:3.13-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1

# System deps for lxml / trafilatura
RUN apt-get update && apt-get install -y --no-install-recommends \
    libxml2 libxslt1.1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run as a non-root user
RUN useradd --no-create-home --shell /bin/false appuser \
    && chown -R appuser /app
USER appuser

# Run migrations then the pipeline. alembic upgrade head is idempotent.
CMD ["sh", "-c", "alembic upgrade head && python -m src.pipeline"]
