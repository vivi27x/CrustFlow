FROM python:3.13-slim
RUN pip install --no-cache-dir uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY app ./app
ENV DB_PATH=/data/crustflow.sqlite REPLAY_DIR=/data/replay
# One worker on purpose: the rate limiter and the live event bus live in this process.
CMD ["sh", "-c", "uv run --no-dev uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
