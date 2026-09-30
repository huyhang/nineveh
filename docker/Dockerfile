# syntax=docker/dockerfile:1.7
FROM python:3.14.7-slim-bookworm AS build

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements-build.lock ./
RUN python -m pip install --no-cache-dir --require-hashes -r requirements-build.lock

COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip wheel --no-cache-dir --no-build-isolation --no-deps \
    --wheel-dir /wheels .

FROM python:3.14.7-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN groupadd --gid 10001 nineveh \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin nineveh \
    && mkdir -p /data /state \
    && chown nineveh:nineveh /state

COPY requirements.lock ./
RUN python -m pip install --no-cache-dir --require-hashes -r requirements.lock

COPY --from=build /wheels/nineveh-*.whl /tmp/
RUN python -m pip install --no-cache-dir --no-deps /tmp/nineveh-*.whl \
    && rm /tmp/nineveh-*.whl

USER 10001:10001
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/v1/health/live', timeout=3)"]

CMD ["python", "-m", "nineveh"]
