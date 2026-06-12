#!/usr/bin/env bash
# Pull the telemetry image from registry and deploy the full local stack via Compose.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

IMAGE_NAME="${IMAGE_NAME:?IMAGE_NAME is required (e.g. docker.io/username/telemetry-pipeline:latest)}"
COMPOSE_FILE="${COMPOSE_FILE:-${PROJECT_ROOT}/compose/docker-compose.deploy.yml}"
DOCKER_REGISTRY_HOST="${DOCKER_REGISTRY_HOST:-docker.io}"

export TELEMETRY_IMAGE="${IMAGE_NAME}"

cd "${PROJECT_ROOT}"

if [[ ! "${COMPOSE_FILE}" = /* ]]; then
    COMPOSE_FILE="${PROJECT_ROOT}/${COMPOSE_FILE}"
fi

registry_login() {
    if [[ -n "${DOCKER_USERNAME:-}" && -n "${DOCKER_PASSWORD:-}" ]]; then
        echo "Logging in to ${DOCKER_REGISTRY_HOST}"
        echo "${DOCKER_PASSWORD}" | docker login "${DOCKER_REGISTRY_HOST}" \
            -u "${DOCKER_USERNAME}" --password-stdin
    fi
}

MAX_RETRIES="${MAX_RETRIES:-5}"
BASE_DELAY_SECONDS="${BASE_DELAY_SECONDS:-10}"

retry() {
    local description="$1"
    shift
    local attempt=1
    local delay="${BASE_DELAY_SECONDS}"

    while [[ "${attempt}" -le "${MAX_RETRIES}" ]]; do
        echo "[retry] ${description} (attempt ${attempt}/${MAX_RETRIES})"
        if "$@"; then
            return 0
        fi
        if [[ "${attempt}" -eq "${MAX_RETRIES}" ]]; then
            echo "[retry] ${description} failed after ${MAX_RETRIES} attempts" >&2
            return 1
        fi
        sleep "${delay}"
        attempt=$((attempt + 1))
        delay=$((delay * 2))
    done
}

retry "Docker Hub login" registry_login

echo "Pulling telemetry application image: ${IMAGE_NAME}"
retry "docker pull" docker pull "${IMAGE_NAME}"

echo "Stopping any existing telemetry stack"
docker compose -f "${COMPOSE_FILE}" down --remove-orphans 2>/dev/null || true

echo "Starting telemetry stack from ${COMPOSE_FILE}"
docker compose -f "${COMPOSE_FILE}" pull
docker compose -f "${COMPOSE_FILE}" up -d

echo "Deployment complete. Running containers:"
docker compose -f "${COMPOSE_FILE}" ps
