FROM python:3.11-slim

WORKDIR /app

# Install uv
RUN pip install --no-cache-dir uv

# Copy project files
COPY pyproject.toml .
COPY expense_tracker/ ./expense_tracker/

# Install dependencies (no dev extras)
RUN uv sync --no-dev

# Data directory (mount a persistent disk here on Render)
RUN mkdir -p /data/exports

ENV EXPENSE_DB_PATH=/data/expenses.db
ENV EXPENSE_EXPORTS_DIR=/data/exports
ENV PRODUCTION=1

EXPOSE 8080

CMD ["uv", "run", "expense-web"]
