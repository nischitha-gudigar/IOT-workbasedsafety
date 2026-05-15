"""
simulator/transport/mqtt_ingestor.py
------------------------------------
MQTT ingestion service for rules-engine-ready telemetry.

Responsibilities:
  1. Subscribe to MQTT topics with reconnect retry.
  2. Validate topic and payload contract for every message.
  3. Route valid messages to a time-series DB (InfluxDB).
  4. Route invalid messages to a dead-letter stream with reason.
  5. Add reliability basics (queue, batch writes, health metrics).

This service is intentionally separate from the simulator publisher.
Downstream rules engines should read cleaned data from DB measurements,
not raw MQTT broker traffic.
"""

from __future__ import annotations

import json
import os
import queue
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Thread
from typing import Any

import paho.mqtt.client as mqtt
from influxdb_client import InfluxDBClient, Point, WriteOptions

from simulator.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class _InboundMessage:
    topic: str
    payload_raw: str
    received_at_utc: str


@dataclass
class _ValidationResult:
    valid: bool
    reason: str | None
    topic_parts: list[str]
    payload: dict[str, Any] | None
    timestamp_utc: datetime | None


class MQTTIngestor:
    """MQTT subscriber + validator + queued batch writer to InfluxDB."""

    def __init__(self, mqtt_config: dict[str, Any]) -> None:
        broker_cfg = mqtt_config.get("broker", {})
        conn_cfg = mqtt_config.get("connection", {})
        topic_cfg = mqtt_config.get("topic", {})
        ingest_cfg = mqtt_config.get("ingestion", {})

        self._host = os.environ.get("MQTT_HOST", broker_cfg.get("host", "localhost"))
        self._port = int(os.environ.get("MQTT_PORT", broker_cfg.get("port", 1883)))
        self._username = os.environ.get("MQTT_USERNAME", broker_cfg.get("username", "")) or None
        self._password = os.environ.get("MQTT_PASSWORD", broker_cfg.get("password", "")) or None

        self._keepalive = int(conn_cfg.get("keepalive_seconds", 60))
        self._clean_session = bool(conn_cfg.get("clean_session", True))
        self._max_reconnects = int(conn_cfg.get("max_reconnect_attempts", 0))
        self._base_reconnect_delay = float(conn_cfg.get("reconnect_delay_seconds", 2.0))

        topic_prefix = os.environ.get("MQTT_TOPIC_PREFIX", topic_cfg.get("prefix", "iots"))
        self._subscription_topic = str(
            os.environ.get(
                "MQTT_INGEST_TOPIC_SUBSCRIPTION",
                ingest_cfg.get("topic_subscription", f"{topic_prefix}/#"),
            )
        )
        self._topic_min_segments = int(ingest_cfg.get("topic_min_segments", 4))
        self._topic_max_segments = int(ingest_cfg.get("topic_max_segments", 6))

        self._queue_maxsize = int(ingest_cfg.get("queue_max_size", 10000))
        self._batch_size = int(ingest_cfg.get("batch_size", 500))
        self._flush_interval_seconds = float(ingest_cfg.get("flush_interval_seconds", 1.0))
        self._health_log_interval_seconds = float(
            ingest_cfg.get("health_log_interval_seconds", 15.0)
        )

        self._valid_measurement = str(ingest_cfg.get("valid_measurement", "raw_telemetry"))
        self._invalid_measurement = str(ingest_cfg.get("invalid_measurement", "dead_letter"))
        self._dead_letter_file = Path(str(ingest_cfg.get("dead_letter_file", "logs/dead_letter.log")))

        self._required_fields: dict[str, tuple[type, ...]] = {
            "value": (int, float),
            "unit": (str,),
            "quality": (str,),
            "fault_active": (bool,),
        }

        influx_url = os.environ.get("INFLUXDB_URL", "http://localhost:8086")
        influx_token = os.environ.get("INFLUXDB_TOKEN", "")
        influx_org = os.environ.get("INFLUXDB_ORG", "iot_org")
        influx_bucket = os.environ.get("INFLUXDB_BUCKET", "iot_poc")

        if not influx_token:
            raise RuntimeError("INFLUXDB_TOKEN must be set for MQTT ingestion")

        self._influx_bucket = influx_bucket
        self._influx_client = InfluxDBClient(url=influx_url, token=influx_token, org=influx_org)
        self._write_api = self._influx_client.write_api(
            write_options=WriteOptions(
                batch_size=max(self._batch_size, 1),
                flush_interval=int(max(self._flush_interval_seconds * 1000, 100)),
                jitter_interval=200,
                retry_interval=1_000,
                max_retries=5,
                max_retry_delay=30_000,
                exponential_base=2,
            )
        )

        self._queue: queue.Queue[_InboundMessage] = queue.Queue(maxsize=self._queue_maxsize)
        self._stop_event = Event()
        self._writer_thread: Thread | None = None

        self._metrics: dict[str, int] = {
            "received": 0,
            "queued": 0,
            "queue_dropped": 0,
            "valid": 0,
            "invalid": 0,
            "written_valid": 0,
            "written_invalid": 0,
            "db_write_failures": 0,
        }

        self._client = mqtt.Client(clean_session=self._clean_session)
        if self._username:
            self._client.username_pw_set(self._username, self._password)

        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

    def start(self) -> None:
        """Start MQTT consumption and DB writer loop."""
        self._dead_letter_file.parent.mkdir(parents=True, exist_ok=True)
        self._writer_thread = Thread(target=self._writer_loop, name="mqtt_ingestion_writer", daemon=True)
        self._writer_thread.start()

        self._connect_with_retry()
        self._client.loop_start()

        logger.info(
            "MQTT ingestion started",
            extra={
                "event": "ingestion_started",
                "host": self._host,
                "port": self._port,
                "subscription": self._subscription_topic,
                "queue_max_size": self._queue_maxsize,
                "batch_size": self._batch_size,
            },
        )

    def stop(self) -> None:
        """Stop ingestion and flush remaining queued messages."""
        self._stop_event.set()

        self._client.loop_stop()
        self._client.disconnect()

        if self._writer_thread is not None:
            self._writer_thread.join(timeout=5.0)

        self._write_api.flush()
        self._influx_client.close()

        logger.info(
            "MQTT ingestion stopped",
            extra={"event": "ingestion_stopped", **self._metrics, "queue_size": self._queue.qsize()},
        )

    def run_forever(self) -> None:
        """Convenience blocking loop for service execution."""
        self.start()
        try:
            while not self._stop_event.is_set():
                time.sleep(1.0)
        except KeyboardInterrupt:
            logger.info("Keyboard interrupt received", extra={"event": "ingestion_interrupt"})
        finally:
            self.stop()

    def _connect_with_retry(self) -> None:
        attempts = 0
        delay = self._base_reconnect_delay
        while True:
            try:
                self._client.connect(self._host, self._port, keepalive=self._keepalive)
                return
            except Exception as exc:
                attempts += 1
                should_stop = self._max_reconnects > 0 and attempts >= self._max_reconnects
                logger.error(
                    "MQTT connect failed",
                    extra={
                        "event": "ingestion_connect_failed",
                        "attempt": attempts,
                        "error": str(exc),
                        "retry_delay_seconds": delay,
                    },
                )
                if should_stop:
                    raise RuntimeError("Unable to connect to MQTT broker after retries") from exc
                time.sleep(delay)
                delay = min(delay * 2.0, 30.0)

    def _on_connect(self, _client, _userdata, _flags, rc) -> None:
        if rc != 0:
            logger.error("MQTT connect refused", extra={"event": "ingestion_connect_refused", "rc": rc})
            return

        result, _mid = self._client.subscribe(self._subscription_topic, qos=1)
        if result != mqtt.MQTT_ERR_SUCCESS:
            logger.error(
                "MQTT subscribe failed",
                extra={"event": "ingestion_subscribe_failed", "topic": self._subscription_topic, "rc": result},
            )
            return

        logger.info(
            "MQTT ingestion connected",
            extra={"event": "ingestion_connected", "subscription": self._subscription_topic},
        )

    def _on_disconnect(self, _client, _userdata, rc) -> None:
        if rc == 0:
            logger.info("MQTT ingestion disconnected", extra={"event": "ingestion_disconnected"})
            return
        logger.warning(
            "MQTT ingestion unexpected disconnect",
            extra={"event": "ingestion_unexpected_disconnect", "rc": rc},
        )

    def _on_message(self, _client, _userdata, msg) -> None:
        self._metrics["received"] += 1

        payload_raw = msg.payload.decode("utf-8", errors="replace")
        inbound = _InboundMessage(
            topic=msg.topic,
            payload_raw=payload_raw,
            received_at_utc=datetime.now(tz=timezone.utc).isoformat(),
        )

        try:
            self._queue.put_nowait(inbound)
            self._metrics["queued"] += 1
        except queue.Full:
            self._metrics["queue_dropped"] += 1
            self._route_invalid(inbound, "ingestion_queue_full")

    def _writer_loop(self) -> None:
        valid_points: list[Point] = []
        invalid_points: list[Point] = []
        last_flush = time.monotonic()
        last_health_log = time.monotonic()

        while not self._stop_event.is_set() or not self._queue.empty():
            try:
                inbound = self._queue.get(timeout=0.2)
            except queue.Empty:
                inbound = None

            if inbound is not None:
                validation = self._validate(inbound)
                if validation.valid and validation.payload and validation.timestamp_utc:
                    valid_points.append(self._to_valid_point(validation))
                    self._metrics["valid"] += 1
                else:
                    reason = validation.reason or "unknown_validation_error"
                    invalid_points.append(self._to_invalid_point(inbound, reason))
                    self._write_dead_letter_file(inbound, reason)
                    self._metrics["invalid"] += 1

            now = time.monotonic()
            should_flush = (
                len(valid_points) + len(invalid_points) >= self._batch_size
                or (now - last_flush) >= self._flush_interval_seconds
            )
            if should_flush:
                self._flush_points(valid_points, invalid_points)
                valid_points.clear()
                invalid_points.clear()
                last_flush = now

            if (now - last_health_log) >= self._health_log_interval_seconds:
                logger.info(
                    "Ingestion health",
                    extra={
                        "event": "ingestion_health",
                        **self._metrics,
                        "queue_size": self._queue.qsize(),
                    },
                )
                last_health_log = now

        if valid_points or invalid_points:
            self._flush_points(valid_points, invalid_points)

    def _flush_points(self, valid_points: list[Point], invalid_points: list[Point]) -> None:
        try:
            if valid_points:
                self._write_api.write(bucket=self._influx_bucket, record=valid_points)
                self._metrics["written_valid"] += len(valid_points)
            if invalid_points:
                self._write_api.write(bucket=self._influx_bucket, record=invalid_points)
                self._metrics["written_invalid"] += len(invalid_points)
        except Exception as exc:
            self._metrics["db_write_failures"] += 1
            logger.error(
                "DB write failed",
                extra={
                    "event": "ingestion_db_write_failed",
                    "error": str(exc),
                    "valid_batch": len(valid_points),
                    "invalid_batch": len(invalid_points),
                },
            )

    def _validate(self, inbound: _InboundMessage) -> _ValidationResult:
        parts = [p for p in inbound.topic.split("/") if p]
        if not (self._topic_min_segments <= len(parts) <= self._topic_max_segments):
            return _ValidationResult(
                valid=False,
                reason=(
                    f"invalid_topic_segments:{len(parts)} expected="
                    f"{self._topic_min_segments}-{self._topic_max_segments}"
                ),
                topic_parts=parts,
                payload=None,
                timestamp_utc=None,
            )

        try:
            raw_payload = json.loads(inbound.payload_raw)
        except json.JSONDecodeError:
            return _ValidationResult(
                valid=False,
                reason="payload_not_valid_json",
                topic_parts=parts,
                payload=None,
                timestamp_utc=None,
            )

        if not isinstance(raw_payload, dict):
            return _ValidationResult(
                valid=False,
                reason="payload_not_object",
                topic_parts=parts,
                payload=None,
                timestamp_utc=None,
            )

        for field_name, expected_types in self._required_fields.items():
            if field_name not in raw_payload:
                return _ValidationResult(
                    valid=False,
                    reason=f"missing_field:{field_name}",
                    topic_parts=parts,
                    payload=None,
                    timestamp_utc=None,
                )
            value = raw_payload[field_name]
            if not isinstance(value, expected_types) or isinstance(value, bool):
                if field_name == "fault_active" and isinstance(value, bool):
                    continue
                return _ValidationResult(
                    valid=False,
                    reason=f"invalid_type:{field_name}",
                    topic_parts=parts,
                    payload=None,
                    timestamp_utc=None,
                )

        timestamp_value = raw_payload.get("timestamp") or raw_payload.get("timestamp_utc")
        if not isinstance(timestamp_value, str):
            return _ValidationResult(
                valid=False,
                reason="missing_or_invalid_timestamp",
                topic_parts=parts,
                payload=None,
                timestamp_utc=None,
            )

        parsed_ts = self._parse_iso8601(timestamp_value)
        if parsed_ts is None:
            return _ValidationResult(
                valid=False,
                reason="timestamp_not_iso8601",
                topic_parts=parts,
                payload=None,
                timestamp_utc=None,
            )

        if "sensor_name" in raw_payload and raw_payload["sensor_name"] != parts[-1]:
            return _ValidationResult(
                valid=False,
                reason="sensor_name_topic_mismatch",
                topic_parts=parts,
                payload=None,
                timestamp_utc=None,
            )

        normalized = dict(raw_payload)
        normalized["value"] = float(raw_payload["value"])
        normalized["unit"] = str(raw_payload["unit"])
        normalized["quality"] = str(raw_payload["quality"])
        normalized["fault_active"] = bool(raw_payload["fault_active"])
        normalized["timestamp"] = parsed_ts.astimezone(timezone.utc).isoformat()

        return _ValidationResult(
            valid=True,
            reason=None,
            topic_parts=parts,
            payload=normalized,
            timestamp_utc=parsed_ts,
        )

    def _to_valid_point(self, validation: _ValidationResult) -> Point:
        payload = validation.payload or {}
        ts = validation.timestamp_utc or datetime.now(tz=timezone.utc)
        parts = validation.topic_parts

        point = Point(self._valid_measurement)
        point.tag("topic", "/".join(parts))
        point.tag("sensor", parts[-1])

        if len(parts) >= 4:
            point.tag("prefix", parts[0])
            point.tag("poc_type", parts[1])
            point.tag("persona_id", parts[2])
        if len(parts) >= 5:
            point.tag("equipment", parts[-2])

        point.field("value", float(payload["value"]))
        point.field("unit", str(payload["unit"]))
        point.field("quality", str(payload["quality"]))
        point.field("fault_active", bool(payload["fault_active"]))

        for optional_field in (
            "phase",
            "condition",
            "device_id",
            "sequence_number",
            "poc_type",
            "persona_id",
            "sensor_name",
        ):
            if optional_field not in payload:
                continue
            value = payload[optional_field]
            if isinstance(value, bool):
                point.field(optional_field, value)
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                point.field(optional_field, float(value))
            else:
                point.field(optional_field, str(value))

        point.time(ts)
        return point

    def _to_invalid_point(self, inbound: _InboundMessage, reason: str) -> Point:
        ts = datetime.now(tz=timezone.utc)
        return (
            Point(self._invalid_measurement)
            .tag("topic", inbound.topic)
            .field("reason", reason)
            .field("payload", inbound.payload_raw)
            .field("received_at_utc", inbound.received_at_utc)
            .time(ts)
        )

    def _route_invalid(self, inbound: _InboundMessage, reason: str) -> None:
        self._metrics["invalid"] += 1
        self._write_dead_letter_file(inbound, reason)
        invalid_point = self._to_invalid_point(inbound, reason)
        try:
            self._write_api.write(bucket=self._influx_bucket, record=[invalid_point])
            self._metrics["written_invalid"] += 1
        except Exception as exc:
            self._metrics["db_write_failures"] += 1
            logger.error(
                "Dead-letter DB write failed",
                extra={"event": "ingestion_dead_letter_write_failed", "error": str(exc)},
            )

    def _write_dead_letter_file(self, inbound: _InboundMessage, reason: str) -> None:
        dead_record = {
            "timestamp_utc": datetime.now(tz=timezone.utc).isoformat(),
            "topic": inbound.topic,
            "payload": inbound.payload_raw,
            "received_at_utc": inbound.received_at_utc,
            "reason": reason,
        }
        with self._dead_letter_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(dead_record) + "\n")

    @staticmethod
    def _parse_iso8601(value: str) -> datetime | None:
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed