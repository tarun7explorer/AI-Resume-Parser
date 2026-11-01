FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN pip install --no-cache-dir uv
COPY pyproject.toml uv.lock README.md ./
RUN if [ -f uv.lock ] && [ ! -s uv.lock ]; then mv uv.lock /tmp/uv.lock.empty; fi && uv lock && uv sync --frozen --no-dev
COPY app ./app
COPY scripts ./scripts
COPY db ./db
COPY static ./static
CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
