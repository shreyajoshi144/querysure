# syntax=docker/dockerfile:1
# Pinned runtime image; the builder is discarded so build tooling never ships.
FROM python:3.12.8-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install only production/runtime dependencies into a self-contained prefix.
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --prefix=/install -r requirements.txt

# The source tree is used only during the build stage.  In particular,
# create_retail.py generates the deterministic sample database that the app
# expects on a fresh deployment.
COPY . .
RUN python create_retail.py
# Content-hashed, minified frontend assets (scripts/build_static.py). Build-only deps stay in the discarded builder stage.
RUN python -m pip install --no-cache-dir -r requirements-build.txt && python scripts/build_static.py

FROM python:3.12.8-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=5000

WORKDIR /app

# Runtime dependencies only; no compiler/toolchain is copied from the builder.
COPY --from=builder /install /usr/local

# Application/runtime files. Tests, CI metadata, and development files are
# intentionally not copied into the production image.
# Every top-level module (api, auth, billing_service, account_service, ops_api, quotas, ai_guard, report_storage, ...).
# A glob, not a hand-kept list: Sprint 3/4 added modules that an explicit list silently omitted, which made the image fail
# at import time. tests/test_sprint5_packaging.py guards this.
COPY --from=builder /app/*.py /app/alembic.ini ./
COPY --from=builder /app/observability ./observability
COPY --from=builder /app/scripts ./scripts
COPY --from=builder /app/modules ./modules
COPY --from=builder /app/migrations ./migrations
COPY --from=builder /app/static ./static
COPY --from=builder /app/evaluation ./evaluation
COPY --from=builder /app/retail.db ./retail.db

# The app writes uploaded datasets, cache files, generated reports, and any
# locally provisioned sample data here. Keep these paths writable by the
# non-root runtime user.
RUN groupadd --system --gid 10001 querysure \
    && useradd --system --uid 10001 --gid 10001 --home-dir /app --no-create-home querysure \
    && mkdir -p /app/uploads/.cache /app/output /app/sample_db \
    && chown -R 10001:10001 /app

USER 10001:10001

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"5000\")}/api/health', timeout=3).read()" || exit 1

CMD ["sh", "-c", "if [ \"$ENVIRONMENT\" = \"production\" ]; then alembic upgrade head || exit 1; fi; exec uvicorn api:app --host 0.0.0.0 --port ${PORT} --workers ${WEB_CONCURRENCY:-2} --proxy-headers --forwarded-allow-ips=\"${TRUSTED_PROXY_IPS}\""]
