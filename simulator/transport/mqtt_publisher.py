"""
simulator/transport/mqtt_publisher.py
---------------------------------------
Thin async MQTT publisher for the Vitals Simulator.

Connects to the MQTT broker configured via environment variables
(falling back to mqtt_config.yaml defaults). Publishes each
SensorReading as a JSON payload to the correct topic.

Topic pattern:
    {prefix}/{poc_type}/{persona_id}/{sensor_name}
    e.g. iots/worker_safety/welder_factory/heart_rate

Payload schema (all fields, always present):
    {
        "timestamp_utc":   "2026-04-06T10:42:15.123456+00:00",
        "device_id":       "sim-welder_factory-001",
        "persona_id":      "welder_factory",
        "poc_type":        "worker_safety",
        "sensor_name":     "heart_rate",
        "value":           84.2,
        "unit":            "bpm",
        "phase":           "early_fatigue",
        "condition":       "normal",
        "quality":         "good",
        "fault_active":    false,
        "sequence_number": 4217
    }

Reconnect behaviour:
    On disconnect, the publisher logs an error and retries with
    exponential backoff. Never crashes the engine on broker issues.
    Buffering is handled by paho-mqtt's internal outgoing queue.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any

import paho.mqtt.client as mqtt

from simulator.sensors.base import SensorReading
from simulator.utils.logger import get_logger

logger = get_logger(__name__)


class MQTTPublisher:
    """
    Async-compatible MQTT publisher using paho-mqtt.

    Connection details are resolved in this order:
      1. Environment variables (MQTT_HOST, MQTT_PORT, etc.)
      2. mqtt_config.yaml values (passed as mqtt_config dict)

    Args:
        mqtt_config: Parsed mqtt_config.yaml dict.
    """

    def __init__(self, mqtt_config: dict[str, Any]) -> None:
        broker_cfg  = mqtt_config.get("broker", {})
        conn_cfg    = mqtt_config.get("connection", {})
        publish_cfg = mqtt_config.get("publish", {})
        topic_cfg   = mqtt_config.get("topic", {})

        self._host      = os.environ.get("MQTT_HOST",     broker_cfg.get("host", "localhost"))
        self._port      = int(os.environ.get("MQTT_PORT", broker_cfg.get("port", 1883)))
        self._username  = os.environ.get("MQTT_USERNAME", broker_cfg.get("username", "")) or None
        self._password  = os.environ.get("MQTT_PASSWORD", broker_cfg.get("password", "")) or None
        self._prefix    = os.environ.get("MQTT_TOPIC_PREFIX", topic_cfg.get("prefix", "iots"))

        self._keepalive     = int(conn_cfg.get("keepalive_seconds", 60))
        self._reconnect_delay = int(conn_cfg.get("reconnect_delay_seconds", 5))
        self._max_reconnects  = int(conn_cfg.get("max_reconnect_attempts", 10))
        self._clean_session   = bool(conn_cfg.get("clean_session", True))

        self._qos    = int(publish_cfg.get("qos", 1))
        self._retain = bool(publish_cfg.get("retain", False))

        self._client = mqtt.Client(clean_session=self._clean_session)
        self._connected = False
        self._reconnect_count = 0

        self._client.on_connect    = self._on_connect
        self._client.on_disconnect = self._on_disconnect

        if self._username:
            self._client.username_pw_set(self._username, self._password)

    def connect(self) -> None:
        """
        Establish connection to the MQTT broker.

        Starts the paho network loop in a background thread.
        """
        try:
            self._client.connect(self._host, self._port, keepalive=self._keepalive)
            self._client.loop_start()
            logger.info(
                "MQTT connecting",
                extra={
                    "event":  "mqtt_connecting",
                    "host":   self._host,
                    "port":   self._port,
                    "prefix": self._prefix,
                },
            )
        except Exception as exc:
            logger.error(
                "MQTT connection failed",
                extra={"event": "mqtt_connect_error", "error": str(exc)},
            )

    def stop(self) -> None:
        """Disconnect from broker and stop background loop."""
        self._client.loop_stop()
        self._client.disconnect()
        logger.info("MQTT disconnected", extra={"event": "mqtt_disconnected"})

    def publish(
        self,
        reading:       SensorReading,
        persona_id:    str,
        poc_type:      str,
        device_id:     str,
        sequence_num:  int = 0,
    ) -> None:
        """
        Publish a SensorReading to the broker.

        Topic is built from prefix/poc_type/persona_id/sensor_name.
        Payload is JSON-encoded.

        On failure: logs error and returns. Never raises.
        """
        topic = f"{self._prefix}/{poc_type}/{persona_id}/{reading.sensor_name}"

        payload = self._build_payload(reading, persona_id, poc_type, device_id, sequence_num)

        try:
            result = self._client.publish(
                topic,
                json.dumps(payload),
                qos=self._qos,
                retain=self._retain,
            )
            if result.rc != mqtt.MQTT_ERR_SUCCESS:
                logger.error(
                    "MQTT publish failed",
                    extra={
                        "event":  "mqtt_publish_error",
                        "topic":  topic,
                        "rc":     result.rc,
                    },
                )
        except Exception as exc:
            logger.error(
                "MQTT publish exception",
                extra={"event": "mqtt_publish_exception", "topic": topic, "error": str(exc)},
            )

    def _build_payload(
        self,
        reading:      SensorReading,
        persona_id:   str,
        poc_type:     str,
        device_id:    str,
        sequence_num: int,
    ) -> dict[str, Any]:
        """Construct the MQTT payload dict following the defined schema."""
        return {
            "timestamp_utc":   datetime.now(tz=timezone.utc).isoformat(),
            "device_id":       device_id,
            "persona_id":      persona_id,
            "poc_type":        poc_type,
            "sensor_name":     reading.sensor_name,
            "value":           reading.value,
            "unit":            reading.unit,
            "phase":           reading.phase,
            "condition":       reading.condition,
            "quality":         reading.quality,
            "fault_active":    reading.fault_active,
            "sequence_number": sequence_num,
        }

    # ------------------------------------------------------------------
    # paho callbacks
    # ------------------------------------------------------------------

    def _on_connect(self, client, userdata, flags, rc) -> None:
        if rc == 0:
            self._connected = True
            self._reconnect_count = 0
            logger.info(
                "MQTT connected",
                extra={"event": "mqtt_connected", "host": self._host, "port": self._port},
            )
        else:
            logger.error(
                "MQTT connection refused",
                extra={"event": "mqtt_connect_refused", "rc": rc},
            )

    def _on_disconnect(self, client, userdata, rc) -> None:
        self._connected = False
        if rc != 0:
            self._reconnect_count += 1
            logger.warning(
                "MQTT unexpected disconnect — will retry",
                extra={
                    "event":            "mqtt_disconnect",
                    "rc":               rc,
                    "reconnect_attempt": self._reconnect_count,
                    "max_attempts":      self._max_reconnects,
                },
            )
