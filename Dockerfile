FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock README.md ./
COPY expense_tracker/ ./expense_tracker/

RUN uv sync --no-dev

ENV PRODUCTION=1

EXPOSE 8080

CMD ["uv", "run", "expense-web"]
