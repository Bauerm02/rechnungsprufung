FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY tests ./tests

RUN python -m pip install --upgrade pip && \
    python -m pip install .[dev]

CMD ["uvicorn", "invoice_automation.api.app:app", "--host", "0.0.0.0", "--port", "8000"]

