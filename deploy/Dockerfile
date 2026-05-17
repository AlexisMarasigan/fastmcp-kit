# Multi-stage build. The Knative Functions buildpack path is also
# supported (see deploy/func.yaml-equivalent); this Dockerfile is the
# fallback for `docker build` and CI smoke tests.

FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN pip install --no-cache-dir uv==0.8.18

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src

# Build the wheel + install the project plus the [observability] extra
# into /install via `uv pip install --target`. No venv, no editable
# installs — a clean tree we can COPY into the runtime image.
#
# The default image bundles `--extra observability` so Prometheus
# exposition and the dashboard generator work out-of-the-box. `[redis]`
# stays opt-in to avoid forcing the Upstash client on stdio users.
RUN uv build --wheel --out-dir /wheels \
 && WHEEL=$(ls /wheels/mcp_toolkit-*.whl) \
 && pip install --no-cache-dir --prefix=/install "${WHEEL}[observability]"


FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

RUN useradd --create-home --uid 1001 app
USER app
WORKDIR /app

COPY --from=builder /install /usr/local

EXPOSE 8080
CMD ["uvicorn", "mcp_toolkit.apps.server.main:app", "--host", "0.0.0.0", "--port", "8080"]
