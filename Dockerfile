FROM python:3.11-slim

WORKDIR /app

# Build dependencies needed by some packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock README.md ./
COPY expense_tracker/ ./expense_tracker/

RUN uv sync --no-dev

ENV PRODUCTION=1

EXPOSE 8080

CMD ["uv", "run", "expense-web"]
