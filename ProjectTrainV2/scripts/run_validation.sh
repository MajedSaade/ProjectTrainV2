#!/usr/bin/env bash
# End-to-end validation and chaos verification for the telemetry pipeline stack.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
COMPOSE_FILE="${PROJECT_ROOT}/compose/docker-compose.yml"

cd "${PROJECT_ROOT}"

readonly GREEN='\033[0;32m'
readonly RED='\033[0;31m'
readonly YELLOW='\033[1;33m'
readonly BLUE='\033[0;34m'
readonly NC='\033[0m'

PASS_COUNT=0
FAIL_COUNT=0
RESULTS=()

log_info() {
    printf '%b[INFO]%b %s\n' "${BLUE}" "${NC}" "$*"
}

log_pass() {
    PASS_COUNT=$((PASS_COUNT + 1))
    RESULTS+=("PASS: $*")
    printf '%b[PASS]%b %s\n' "${GREEN}" "${NC}" "$*"
}

log_fail() {
    FAIL_COUNT=$((FAIL_COUNT + 1))
    RESULTS+=("FAIL: $*")
    printf '%b[FAIL]%b %s\n' "${RED}" "${NC}" "$*"
}

compose() {
    if docker compose version >/dev/null 2>&1; then
        docker compose -f "${COMPOSE_FILE}" "$@"
    else
        docker-compose -f "${COMPOSE_FILE}" "$@"
    fi
}

assert_log_contains() {
    local pattern="$1"
    local description="$2"
    if grep -q "${pattern}" "${LOG_FILE}"; then
        log_pass "${description}"
    else
        log_fail "${description} (missing pattern: ${pattern})"
    fi
}

wait_for_container_running() {
    local container="$1"
    local attempts="${2:-30}"
    local delay="${3:-2}"

    for _ in $(seq 1 "${attempts}"); do
        if docker inspect -f '{{.State.Status}}' "${container}" 2>/dev/null | grep -q running; then
            return 0
        fi
        sleep "${delay}"
    done
    return 1
}

wait_for_container_exited() {
    local container="$1"
    local attempts="${2:-20}"
    local delay="${3:-1}"

    for _ in $(seq 1 "${attempts}"); do
        local status
        status="$(docker inspect -f '{{.State.Status}}' "${container}" 2>/dev/null || echo missing)"
        if [[ "${status}" == "exited" ]]; then
            return 0
        fi
        sleep "${delay}"
    done
    return 1
}

cleanup_stack() {
    compose down --remove-orphans >/dev/null 2>&1 || true
}

LOG_FILE="$(mktemp)"
trap 'rm -f "${LOG_FILE}"; cleanup_stack' EXIT

log_info "Phase 4 validation starting (chaos mode enabled)"
cleanup_stack

export ENABLE_CHAOS=true
export RUN_DURATION_SECONDS=120

log_info "Building and starting infrastructure in detached mode"
compose up --build -d datastore telemetry-app

log_info "Waiting for telemetry-app container to enter running state"
if wait_for_container_running "telemetry-app"; then
    log_pass "telemetry-app container is running"
else
    log_fail "telemetry-app container failed to start"
fi

log_info "Monitoring application logs for 15 seconds"
sleep 5
compose logs telemetry-app >"${LOG_FILE}" 2>&1
sleep 10
compose logs telemetry-app >>"${LOG_FILE}" 2>&1

assert_log_contains "Datastore reachable" "Entrypoint readiness loop passed"
assert_log_contains "Connected to Redis at" "Redis connection established"
assert_log_contains "Pipeline started" "Worker pool initialized"
assert_log_contains "Consumer started" "Consumer threads came online"

if grep -q "Chaos mode active" "${LOG_FILE}"; then
    log_pass "Chaos instrumentation confirmed in runtime logs"
else
    log_fail "Chaos instrumentation not detected"
fi

log_info "Verifying Redis persistence and monotonic counter growth"
redis_tool_count() {
    compose exec -T datastore redis-cli HGET "${1}" sample_count 2>/dev/null | tr -d '\r'
}

redis_tool_average() {
    compose exec -T datastore redis-cli HGET "${1}" running_average 2>/dev/null | tr -d '\r'
}

baseline_count="$(redis_tool_count "TOOL_01")"
sleep 3
followup_count="$(redis_tool_count "TOOL_01")"

if [[ -n "${baseline_count}" && "${baseline_count}" =~ ^[0-9]+$ ]]; then
    log_pass "Redis key TOOL_01 exists with sample_count=${baseline_count}"
else
    log_fail "Redis key TOOL_01 missing or invalid sample_count"
fi

if [[ -n "${followup_count}" && "${followup_count}" =~ ^[0-9]+$ && "${followup_count}" -gt "${baseline_count}" ]]; then
    log_pass "Redis sample_count is incrementing (${baseline_count} -> ${followup_count})"
else
    log_fail "Redis sample_count did not increment (${baseline_count} -> ${followup_count})"
fi

for tool_id in TOOL_01 TOOL_02 TOOL_03; do
    average="$(redis_tool_average "${tool_id}")"
    if [[ -n "${average}" ]] && awk "BEGIN {exit !(${average} > 0)}"; then
        log_pass "Redis running_average for ${tool_id} is valid (${average})"
    else
        log_fail "Redis running_average for ${tool_id} is missing or invalid"
    fi
done

log_info "Verifying heartbeat file freshness inside telemetry-app"
heartbeat_mtime() {
    compose exec -T telemetry-app python -c "import os; print(os.path.getmtime('/tmp/app_heartbeat'))" 2>/dev/null | tr -d '\r'
}

mtime_a="$(heartbeat_mtime)"
sleep 2
mtime_b="$(heartbeat_mtime)"

if [[ -n "${mtime_a}" && -n "${mtime_b}" ]]; then
    if awk "BEGIN {exit !(${mtime_b} > ${mtime_a})}"; then
        log_pass "Heartbeat file is updating regularly (${mtime_a} -> ${mtime_b})"
    else
        log_fail "Heartbeat file mtime did not advance (${mtime_a} -> ${mtime_b})"
    fi
else
    log_fail "Heartbeat file /tmp/app_heartbeat is missing or unreadable"
fi

log_info "Sending SIGINT to telemetry-app and verifying graceful shutdown"
docker kill -s SIGINT telemetry-app >/dev/null

if wait_for_container_exited "telemetry-app"; then
    log_pass "telemetry-app exited after SIGINT"
else
    log_fail "telemetry-app did not exit cleanly after SIGINT"
fi

exit_code="$(docker inspect -f '{{.State.ExitCode}}' telemetry-app 2>/dev/null || echo 255)"
if [[ "${exit_code}" == "0" ]]; then
    log_pass "telemetry-app shutdown exit code is 0"
else
    log_fail "telemetry-app shutdown exit code is ${exit_code} (expected 0)"
fi

compose logs telemetry-app 2>&1 | tee -a "${LOG_FILE}" >/dev/null
if grep -q "Shutdown requested" "${LOG_FILE}" && grep -q "Pipeline shutdown complete" "${LOG_FILE}"; then
    log_pass "Graceful shutdown sequence recorded in logs"
else
    log_fail "Graceful shutdown markers missing from logs"
fi

printf '\n%b========== VALIDATION REPORT ==========%b\n' "${BLUE}" "${NC}"
for result in "${RESULTS[@]}"; do
    if [[ "${result}" == PASS:* ]]; then
        printf '%b%s%b\n' "${GREEN}" "${result}" "${NC}"
    else
        printf '%b%s%b\n' "${RED}" "${result}" "${NC}"
    fi
done

printf '\nSummary: %b%d passed%b, %b%d failed%b\n' \
    "${GREEN}" "${PASS_COUNT}" "${NC}" \
    "${RED}" "${FAIL_COUNT}" "${NC}"

if [[ "${FAIL_COUNT}" -gt 0 ]]; then
    exit 1
fi

exit 0
