FROM python:3.13-slim

WORKDIR /app

# System deps for asyncpg, catboost, pyarrow
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ libpq-dev && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
COPY src/ src/

RUN pip install --no-cache-dir -e .

CMD ["python", "-m", "alpha_lab.dashboard.api"]
