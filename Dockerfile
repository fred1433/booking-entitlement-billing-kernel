FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY alembic.ini ./
COPY migrations ./migrations
COPY scripts ./scripts

EXPOSE 8000

# The API and the worker are the same image with a different command, which is
# what keeps a scheduled run and a request path from drifting apart.
CMD ["uvicorn", "kernel.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
