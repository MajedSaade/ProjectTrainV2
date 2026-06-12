#!/usr/bin/env bash
# Build and push images with retry logic for transient Docker Hub auth/network failures.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

: "${DOCKER_REGISTRY_HOST:?DOCKER_REGISTRY_HOST is required}"
: "${DOCKER_USERNAME:?DOCKER_USERNAME is required}"
: "${DOCKER_PASSWORD:?DOCKER_PASSWORD is required}"
: "${FULL_IMAGE:?FULL_IMAGE is required}"
: "${FULL_IMAGE_LATEST:?FULL_IMAGE_LATEST is required}"

MAX_RETRIES="${MAX_RETRIES:-5}"
BASE_DELAY_SECONDS="${BASE_DELAY_SECONDS:-10}"

cd "${PROJECT_ROOT}"

retry() {
    local description="$1"
    shift
    local attempt=1
    local delay="${BASE_DELAY_SECONDS}"

    while [[ "${attempt}" -le "${MAX_RETRIES}" ]]; do
        echo "[retry] ${description} (attempt ${attempt}/${MAX_RETRIES})"
        if "$@"; then
            echo "[retry] ${description} succeeded"
            return 0
        fi

        if [[ "${attempt}" -eq "${MAX_RETRIES}" ]]; then
            echo "[retry] ${description} failed after ${MAX_RETRIES} attempts" >&2
            return 1
        fi

        echo "[retry] ${description} failed; sleeping ${delay}s before retry"
        sleep "${delay}"
        attempt=$((attempt + 1))
        delay=$((delay * 2))
    done
}

registry_login() {
    echo "${DOCKER_PASSWORD}" | docker login "${DOCKER_REGISTRY_HOST}" \
        -u "${DOCKER_USERNAME}" --password-stdin
}

echo "Building image tags: ${FULL_IMAGE}, ${FULL_IMAGE_LATEST}"
docker build -t "${FULL_IMAGE}" -t "${FULL_IMAGE_LATEST}" .

retry "Docker Hub login" registry_login
retry "docker push ${FULL_IMAGE}" docker push "${FULL_IMAGE}"
retry "docker push ${FULL_IMAGE_LATEST}" docker push "${FULL_IMAGE_LATEST}"

echo "Publish complete: ${FULL_IMAGE_LATEST}"
