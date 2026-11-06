# The Blindfold engine: the image that runs inside the enclave.
# Build from the repo root: docker compose build engine
FROM python:3.14.9-slim-bookworm@sha256:945c2676dc2b18e0c1f16879244122c57c0ffede60006bf3df159c507a31bcae

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
# Every dependency, direct or not, pinned by version and sha256 (engine/requirements.lock, `make lock`)
COPY engine/requirements.lock engine/requirements.lock
RUN pip install --no-cache-dir --require-hashes --no-deps -r engine/requirements.lock
COPY engine/pyproject.toml engine/pyproject.toml
COPY engine/blindfold engine/blindfold
ENV PYTHONPATH=/app/engine
COPY datasets/riverside datasets/riverside

EXPOSE 8000
CMD ["uvicorn", "blindfold.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]