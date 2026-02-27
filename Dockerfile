FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=0

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    build-essential \
    libpq-dev \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# keep requirements in its own layer so it caches
COPY requirements.txt .

# cache pip downloads + built wheels between builds (BuildKit)
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

COPY *.py .
COPY tools/ tools/

EXPOSE 8766
CMD ["python", "-m", "server"]