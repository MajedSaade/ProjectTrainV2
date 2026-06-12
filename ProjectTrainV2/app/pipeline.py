"""Multi-threaded semiconductor telemetry pipeline with Redis persistence."""

from __future__ import annotations

import logging
import os
import queue
import random
import signal
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Final

import redis
from redis.exceptions import RedisError

METRIC_NAMES: Final[tuple[str, ...]] = (
    "LayerThickness",
    "EtchRate",
    "ChamberPressure",
)
TOOL_IDS: Final[tuple[str, ...]] = ("TOOL_01", "TOOL_02", "TOOL_03")
QUEUE_MAXSIZE: Final[int] = 100
CONSUMER_COUNT: Final[int] = 4
PRODUCER_COUNT: Final[int] = 3
QUEUE_GET_TIMEOUT: Final[float] = 1.0
PRODUCER_MIN_INTERVAL: Final[float] = 0.010
PRODUCER_MAX_INTERVAL: Final[float] = 0.050
VALUE_MIN: Final[float] = 10.0
VALUE_MAX: Final[float] = 150.0
DEFAULT_RUN_DURATION_SECONDS: Final[float] = 10.0
REDIS_CONNECT_RETRIES: Final[int] = 10
REDIS_CONNECT_RETRY_DELAY: Final[float] = 1.0
DEFAULT_HEARTBEAT_PATH: Final[str] = "/tmp/app_heartbeat"
NORMAL_ERROR_RATE: Final[float] = 0.05
CHAOS_ERROR_RATE: Final[float] = 0.40
CHAOS_MALFORMED_RATE: Final[float] = 0.10
CHAOS_BURST_DURATION_SECONDS: Final[float] = 2.0
CHAOS_BURST_MIN_INTERVAL_SECONDS: Final[float] = 3.0
CHAOS_BURST_MAX_INTERVAL_SECONDS: Final[float] = 6.0


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(threadName)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


def _chaos_enabled() -> bool:
    return os.getenv("ENABLE_CHAOS", "false").strip().lower() == "true"


@dataclass(frozen=True)
class RedisConfig:
    """Redis connection settings loaded from environment variables."""

    host: str
    port: int
    db: int
    password: str | None
    socket_timeout: float

    @classmethod
    def from_env(cls) -> RedisConfig:
        password = os.getenv("REDIS_PASSWORD")
        return cls(
            host=os.getenv("REDIS_HOST", "localhost"),
            port=int(os.getenv("REDIS_PORT", "6379")),
            db=int(os.getenv("REDIS_DB", "0")),
            password=password if password else None,
            socket_timeout=float(os.getenv("REDIS_SOCKET_TIMEOUT", "2.0")),
        )


@dataclass(frozen=True)
class TelemetryRecord:
    """Structured telemetry log payload."""

    timestamp: str
    tool_id: str
    metric_name: str
    value: float
    status_code: int

    def format_line(self) -> str:
        return (
            f"{self.timestamp} | {self.tool_id} | {self.metric_name} | "
            f"{self.value:.2f} | {self.status_code}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "tool_id": self.tool_id,
            "metric_name": self.metric_name,
            "value": self.value,
            "status_code": self.status_code,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> TelemetryRecord:
        return cls(
            timestamp=str(payload["timestamp"]),
            tool_id=str(payload["tool_id"]),
            metric_name=str(payload["metric_name"]),
            value=float(payload["value"]),
            status_code=int(payload["status_code"]),
        )

    @classmethod
    def from_log_line(cls, line: str) -> TelemetryRecord:
        if not line.strip():
            raise ValueError("Empty telemetry log line")

        parts = [part.strip() for part in line.split("|")]
        if len(parts) != 5:
            raise ValueError(f"Invalid telemetry log format: {line!r}")

        return cls(
            timestamp=parts[0],
            tool_id=parts[1],
            metric_name=parts[2],
            value=float(parts[3]),
            status_code=int(parts[4]),
        )


@dataclass(frozen=True)
class QuarantinedItem:
    """Corrupt or unparseable payload isolated from the healthy processing path."""

    source: str
    detail: str
    raw_payload: str


QueuePayload = TelemetryRecord | dict[str, Any] | str
ErrorChannelItem = TelemetryRecord | QuarantinedItem


class ChaosEngine:
    """Injects malformed payloads and high-frequency error bursts for resilience testing."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._burst_until = 0.0
        self._next_burst_at = time.monotonic() + random.uniform(
            CHAOS_BURST_MIN_INTERVAL_SECONDS,
            CHAOS_BURST_MAX_INTERVAL_SECONDS,
        )
        self._logger = logging.getLogger(self.__class__.__name__)
        self._logger.warning(
            "Chaos mode active: %.0f%% malformed injection, error bursts at %.0f%%",
            CHAOS_MALFORMED_RATE * 100,
            CHAOS_ERROR_RATE * 100,
        )

    def _schedule_burst_if_due(self) -> None:
        now = time.monotonic()
        with self._lock:
            if now >= self._next_burst_at:
                self._burst_until = now + CHAOS_BURST_DURATION_SECONDS
                self._next_burst_at = now + random.uniform(
                    CHAOS_BURST_MIN_INTERVAL_SECONDS,
                    CHAOS_BURST_MAX_INTERVAL_SECONDS,
                )
                self._logger.warning(
                    "Chaos error burst engaged for %.1fs (STATUS_CODE 500 -> %.0f%%)",
                    CHAOS_BURST_DURATION_SECONDS,
                    CHAOS_ERROR_RATE * 100,
                )

    def in_burst_window(self) -> bool:
        self._schedule_burst_if_due()
        return time.monotonic() < self._burst_until

    def next_status_code(self) -> int:
        error_rate = CHAOS_ERROR_RATE if self.in_burst_window() else NORMAL_ERROR_RATE
        return 500 if random.random() < error_rate else 200

    def maybe_malformed_payload(self, tool_id: str, timestamp: str) -> str | None:
        if random.random() >= CHAOS_MALFORMED_RATE:
            return None

        malformed_variants = [
            "",
            f"{timestamp} {tool_id} LayerThickness 99.0 200",
            f"{timestamp} | {tool_id} | LayerThickness | not_a_number | 200",
            f"{timestamp} | {tool_id}",
            "||||",
            f"{timestamp} | {tool_id} | LayerThickness | ABC123 | two_hundred",
        ]
        payload = random.choice(malformed_variants)
        self._logger.debug("Injecting malformed chaos payload for %s: %r", tool_id, payload)
        return payload


class HeartbeatMonitor:
    """Disk-based worker heartbeat for container health probes."""

    def __init__(self, heartbeat_path: str) -> None:
        self._heartbeat_path = heartbeat_path
        self._lock = threading.Lock()
        self._logger = logging.getLogger(self.__class__.__name__)

    def touch(self) -> None:
        """Atomically update the heartbeat file with the current UTC timestamp."""
        timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        temp_path = f"{self._heartbeat_path}.tmp"

        try:
            with self._lock:
                with open(temp_path, "w", encoding="utf-8") as handle:
                    handle.write(timestamp)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_path, self._heartbeat_path)
        except OSError as exc:
            self._logger.error("Failed to update heartbeat at %s: %s", self._heartbeat_path, exc)


@dataclass
class RunningAverageTracker:
    """In-memory fallback for per-tool running averages."""

    _sums: dict[str, float] = field(default_factory=dict)
    _counts: dict[str, int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record_value(self, tool_id: str, value: float) -> None:
        with self._lock:
            self._sums[tool_id] = self._sums.get(tool_id, 0.0) + value
            self._counts[tool_id] = self._counts.get(tool_id, 0) + 1

    def snapshot_averages(self) -> dict[str, float]:
        with self._lock:
            return {
                tool_id: self._sums[tool_id] / self._counts[tool_id]
                for tool_id in self._sums
                if self._counts.get(tool_id, 0) > 0
            }


class RedisMetricsStore:
    """Persists per-tool running averages to Redis hashes keyed by TOOL_ID."""

    def __init__(self, config: RedisConfig) -> None:
        self._config = config
        self._client: redis.Redis | None = None
        self._logger = logging.getLogger(self.__class__.__name__)

    @property
    def is_connected(self) -> bool:
        return self._client is not None

    def connect(self) -> bool:
        """Establish a Redis connection with bounded startup retries."""
        for attempt in range(1, REDIS_CONNECT_RETRIES + 1):
            try:
                client = redis.Redis(
                    host=self._config.host,
                    port=self._config.port,
                    db=self._config.db,
                    password=self._config.password,
                    socket_timeout=self._config.socket_timeout,
                    decode_responses=True,
                )
                client.ping()
                self._client = client
                self._logger.info(
                    "Connected to Redis at %s:%s (db=%s)",
                    self._config.host,
                    self._config.port,
                    self._config.db,
                )
                return True
            except RedisError as exc:
                self._logger.warning(
                    "Redis connection attempt %d/%d failed: %s",
                    attempt,
                    REDIS_CONNECT_RETRIES,
                    exc,
                )
                time.sleep(REDIS_CONNECT_RETRY_DELAY)

        self._logger.error(
            "Unable to connect to Redis after %d attempts; using in-memory fallback only",
            REDIS_CONNECT_RETRIES,
        )
        self._client = None
        return False

    def persist_running_average(self, tool_id: str, value: float) -> bool:
        """
        Atomically increment per-tool counters and refresh the running_average field.

        Hash key: TOOL_ID
        Fields: sum, sample_count, running_average
        """
        if self._client is None:
            return False

        try:
            new_sum = self._client.hincrbyfloat(tool_id, "sum", value)
            new_count = self._client.hincrby(tool_id, "sample_count", 1)
            running_average = float(new_sum) / int(new_count)
            self._client.hset(
                tool_id,
                mapping={
                    "running_average": f"{running_average:.6f}",
                    "sum": str(new_sum),
                    "sample_count": str(new_count),
                },
            )
            return True
        except RedisError as exc:
            self._logger.error(
                "Redis persist failed for %s; continuing with in-memory state: %s",
                tool_id,
                exc,
            )
            return False

    def fetch_running_averages(self) -> dict[str, float]:
        """Read persisted running averages for all known tool IDs."""
        if self._client is None:
            return {}

        averages: dict[str, float] = {}
        for tool_id in TOOL_IDS:
            try:
                raw_average = self._client.hget(tool_id, "running_average")
                if raw_average is not None:
                    averages[tool_id] = float(raw_average)
            except RedisError as exc:
                self._logger.error(
                    "Redis read failed for %s; skipping persisted value: %s",
                    tool_id,
                    exc,
                )
        return averages

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except RedisError as exc:
                self._logger.warning("Error while closing Redis client: %s", exc)
            finally:
                self._client = None


class TelemetryPipeline:
    """Coordinates producers, consumers, Redis persistence, and shutdown."""

    def __init__(
        self,
        redis_store: RedisMetricsStore | None = None,
        heartbeat: HeartbeatMonitor | None = None,
        chaos: ChaosEngine | None = None,
    ) -> None:
        self._telemetry_queue: queue.Queue[QueuePayload] = queue.Queue(maxsize=QUEUE_MAXSIZE)
        self._error_channel: queue.Queue[ErrorChannelItem] = queue.Queue()
        self._shutdown_event = threading.Event()
        self._averages = RunningAverageTracker()
        self._redis_store = redis_store
        self._heartbeat = heartbeat
        self._chaos = chaos
        self._producer_threads: list[threading.Thread] = []
        self._consumer_threads: list[threading.Thread] = []
        self._logger = logging.getLogger(self.__class__.__name__)

    @staticmethod
    def _iso_timestamp() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds")

    def _next_status_code(self) -> int:
        if self._chaos is not None:
            return self._chaos.next_status_code()
        return 500 if random.random() < NORMAL_ERROR_RATE else 200

    @staticmethod
    def _normalize_record(payload: QueuePayload) -> TelemetryRecord:
        if isinstance(payload, TelemetryRecord):
            return payload
        if isinstance(payload, dict):
            return TelemetryRecord.from_dict(payload)
        if isinstance(payload, str):
            return TelemetryRecord.from_log_line(payload)
        raise TypeError(f"Unsupported payload type: {type(payload)!r}")

    @staticmethod
    def _payload_to_raw(payload: QueuePayload) -> str:
        if isinstance(payload, TelemetryRecord):
            return payload.format_line()
        if isinstance(payload, dict):
            return str(payload)
        return payload

    def _quarantine_payload(self, payload: QueuePayload, reason: str) -> None:
        item = QuarantinedItem(
            source="parse_error",
            detail=reason,
            raw_payload=self._payload_to_raw(payload),
        )
        self._error_channel.put_nowait(item)
        self._logger.warning(
            "Quarantined corrupt payload (%s): %s",
            reason,
            item.raw_payload,
        )

    def _build_producer_payload(self, tool_id: str) -> QueuePayload:
        timestamp = self._iso_timestamp()

        if self._chaos is not None:
            malformed = self._chaos.maybe_malformed_payload(tool_id, timestamp)
            if malformed is not None:
                return malformed

        return TelemetryRecord(
            timestamp=timestamp,
            tool_id=tool_id,
            metric_name=random.choice(METRIC_NAMES),
            value=round(random.uniform(VALUE_MIN, VALUE_MAX), 2),
            status_code=self._next_status_code(),
        )

    def _producer_loop(self, tool_id: str) -> None:
        self._logger.info("Producer started for %s", tool_id)
        while not self._shutdown_event.is_set():
            payload = self._build_producer_payload(tool_id)
            try:
                self._telemetry_queue.put(payload, timeout=QUEUE_GET_TIMEOUT)
                if isinstance(payload, TelemetryRecord):
                    self._logger.debug("Enqueued %s", payload.format_line())
            except queue.Full:
                if self._shutdown_event.is_set():
                    break
                self._logger.warning("Telemetry queue full; retrying for %s", tool_id)
                continue

            if self._shutdown_event.wait(
                timeout=random.uniform(PRODUCER_MIN_INTERVAL, PRODUCER_MAX_INTERVAL)
            ):
                break

        self._logger.info("Producer stopped for %s", tool_id)

    def _process_record(self, record: TelemetryRecord) -> None:
        if record.status_code == 500:
            self._error_channel.put_nowait(record)
            self._logger.warning(
                "Diverted critical error from %s: %s",
                record.tool_id,
                record.format_line(),
            )
            return

        self._averages.record_value(record.tool_id, record.value)

        if self._redis_store is not None:
            self._redis_store.persist_running_average(record.tool_id, record.value)

        self._logger.debug("Processed healthy record from %s", record.tool_id)

    def _handle_payload(self, payload: QueuePayload) -> None:
        try:
            record = self._normalize_record(payload)
        except (ValueError, TypeError, KeyError) as exc:
            self._quarantine_payload(payload, str(exc))
            return

        self._process_record(record)

    def _consumer_loop(self) -> None:
        self._logger.info("Consumer started")
        while True:
            if self._shutdown_event.is_set() and self._telemetry_queue.empty():
                break

            try:
                payload = self._telemetry_queue.get(timeout=QUEUE_GET_TIMEOUT)
            except queue.Empty:
                continue

            try:
                self._handle_payload(payload)
                if self._heartbeat is not None:
                    self._heartbeat.touch()
            except Exception:
                self._logger.exception(
                    "Unexpected consumer failure; quarantining payload and continuing"
                )
                self._quarantine_payload(payload, "unexpected_processing_failure")
            finally:
                self._telemetry_queue.task_done()

        self._logger.info("Consumer stopped")

    def start(self) -> None:
        if self._producer_threads or self._consumer_threads:
            raise RuntimeError("Pipeline is already running")

        for index in range(PRODUCER_COUNT):
            thread = threading.Thread(
                target=self._producer_loop,
                args=(TOOL_IDS[index],),
                name=f"Producer-{TOOL_IDS[index]}",
                daemon=False,
            )
            self._producer_threads.append(thread)
            thread.start()

        for index in range(CONSUMER_COUNT):
            thread = threading.Thread(
                target=self._consumer_loop,
                name=f"Consumer-{index + 1}",
                daemon=False,
            )
            self._consumer_threads.append(thread)
            thread.start()

        mode = "CHAOS" if self._chaos is not None else "NORMAL"
        self._logger.info(
            "Pipeline started in %s mode with %d producers and %d consumers",
            mode,
            PRODUCER_COUNT,
            CONSUMER_COUNT,
        )

    def shutdown(self) -> None:
        if self._shutdown_event.is_set():
            return

        self._logger.info("Shutdown requested")
        self._shutdown_event.set()

        for thread in self._producer_threads:
            thread.join()

        self._telemetry_queue.join()

        for thread in self._consumer_threads:
            thread.join()

        if self._redis_store is not None:
            self._redis_store.close()

        self._logger.info("Pipeline shutdown complete")

    def run_for(self, duration_seconds: float) -> None:
        self.start()
        end_time = time.monotonic() + duration_seconds
        while time.monotonic() < end_time:
            if self._shutdown_event.is_set():
                break
            time.sleep(0.1)
        self.shutdown()

    def get_running_averages(self) -> dict[str, float]:
        redis_averages = (
            self._redis_store.fetch_running_averages()
            if self._redis_store is not None and self._redis_store.is_connected
            else {}
        )
        if redis_averages:
            return redis_averages
        return self._averages.snapshot_averages()

    def drain_error_channel(self) -> list[ErrorChannelItem]:
        errors: list[ErrorChannelItem] = []
        while True:
            try:
                errors.append(self._error_channel.get_nowait())
            except queue.Empty:
                break
        return errors


def _resolve_run_duration() -> float:
    raw_duration = os.getenv("RUN_DURATION_SECONDS", str(DEFAULT_RUN_DURATION_SECONDS))
    return float(raw_duration)


def _resolve_heartbeat_path() -> str:
    return os.getenv("HEARTBEAT_PATH", DEFAULT_HEARTBEAT_PATH)


def _print_final_report(pipeline: TelemetryPipeline, redis_connected: bool) -> None:
    averages = pipeline.get_running_averages()
    errors = pipeline.drain_error_channel()
    source = "Redis" if redis_connected and averages else "in-memory fallback"

    status_errors = [item for item in errors if isinstance(item, TelemetryRecord)]
    quarantined = [item for item in errors if isinstance(item, QuarantinedItem)]

    print(f"\n=== Final Running Averages (per TOOL_ID) [{source}] ===")
    if not averages:
        print("No healthy telemetry records were processed.")
    else:
        for tool_id in sorted(averages):
            print(f"  {tool_id}: {averages[tool_id]:.4f}")

    print("\n=== Prioritized Error Channel (STATUS_CODE == 500) ===")
    if not status_errors:
        print("No critical status errors captured.")
    else:
        for record in status_errors:
            print(f"  {record.format_line()}")

    print("\n=== Quarantined Corrupt Payloads ===")
    if not quarantined:
        print("No corrupt payloads quarantined.")
    else:
        for item in quarantined:
            print(f"  [{item.source}] {item.detail} -> {item.raw_payload!r}")

    print(f"\nTotal status errors captured: {len(status_errors)}")
    print(f"Total quarantined corrupt payloads: {len(quarantined)}")


def main() -> None:
    configure_logging()
    logger = logging.getLogger(__name__)

    redis_config = RedisConfig.from_env()
    redis_store = RedisMetricsStore(redis_config)
    heartbeat = HeartbeatMonitor(_resolve_heartbeat_path())
    chaos = ChaosEngine() if _chaos_enabled() else None

    logger.info("Main pipeline logic executing")
    redis_connected = redis_store.connect()

    pipeline = TelemetryPipeline(
        redis_store=redis_store,
        heartbeat=heartbeat,
        chaos=chaos,
    )

    def _handle_interrupt(signum: int, _frame: Any) -> None:
        logger.info("Received signal %s; initiating graceful shutdown", signum)
        pipeline.shutdown()

    signal.signal(signal.SIGINT, _handle_interrupt)
    signal.signal(signal.SIGTERM, _handle_interrupt)

    run_duration = _resolve_run_duration()
    try:
        logger.info("Running telemetry pipeline for %.1f seconds", run_duration)
        pipeline.run_for(run_duration)
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt captured; shutting down")
        pipeline.shutdown()
    finally:
        _print_final_report(pipeline, redis_connected=redis_connected)


if __name__ == "__main__":
    main()
