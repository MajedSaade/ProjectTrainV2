# =============================================================================
# Stage 1: Builder
# -----------------------------------------------------------------------------
# Caching strategy:
#   - Copy requirements.txt alone first so dependency layers stay cached when
#     only application source changes.
#   - Install wheels into a dedicated virtualenv (/opt/venv) that is copied
#     wholesale into the runtime image, avoiding repeated pip work at run time.
# =============================================================================
FROM python:3.11-slim AS builder

WORKDIR /build

RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt


# =============================================================================
# Stage 2: Runtime
# -----------------------------------------------------------------------------
# Security posture:
#   - Create a dedicated non-root group/user before copying application files.
#   - chown the working directory and virtualenv so the runtime user can read
#     dependencies and execute the pipeline without elevated privileges.
#   - Switch to USER telemetry_user before CMD so no process runs as root.
# =============================================================================
FROM python:3.11-slim AS runtime

RUN groupadd --gid 1001 telemetry_group \
    && useradd --uid 1001 --gid telemetry_group --create-home telemetry_user

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY app/ ./app/
COPY docker/entrypoint.sh /app/entrypoint.sh

# Non-root permission management: grant ownership of runtime paths only.
RUN chmod +x /app/entrypoint.sh \
    && chown -R telemetry_user:telemetry_group /app /opt/venv \
    && rm -rf /var/lib/apt/lists/*

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER telemetry_user

ENTRYPOINT ["/app/entrypoint.sh"]
