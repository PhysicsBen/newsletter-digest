FROM python:3.12-slim

WORKDIR /app

# System deps for lxml / trafilatura
RUN apt-get update && apt-get install -y --no-install-recommends \
    libxml2 libxslt1.1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run migrations then the pipeline. alembic upgrade head is idempotent.
CMD ["sh", "-c", "alembic upgrade head && python -m src.pipeline"]
