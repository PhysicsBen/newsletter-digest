FROM python:3.13-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1
# Point HuggingFace / sentence-transformers cache to a path inside /app
# (appuser has no home dir, so ~/.cache would fail)
ENV HF_HOME=/app/.cache
ENV SENTENCE_TRANSFORMERS_HOME=/app/.cache/sentence-transformers

# System deps for lxml / trafilatura
RUN apt-get update && apt-get install -y --no-install-recommends \
    libxml2 libxslt1.1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# Install CPU-only PyTorch first to avoid pulling the multi-GB CUDA build
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run as a non-root user
RUN useradd --no-create-home --shell /bin/false appuser \
    && mkdir -p /app/.cache \
    && chown -R appuser /app
USER appuser

# Run migrations then the pipeline. alembic upgrade head is idempotent.
# Merge stderr into stdout (2>&1) so Railway's log viewer captures everything in one stream.
# Echo the exit code explicitly so crashes are visible even if Python swallows the traceback.
CMD ["sh", "-c", "alembic upgrade head 2>&1 && echo '[startup] alembic done, launching pipeline' && python -u -m src.pipeline 2>&1; EXIT=$?; echo \"[startup] pipeline exited with code $EXIT\"; exit $EXIT"]
