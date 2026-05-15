"""
run_ingestion.py
----------------
Entry point for MQTT ingestion service.

Usage:
    python run_ingestion.py [--config config/mqtt_config.yaml]
"""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml

from simulator.transport.mqtt_ingestor import MQTTIngestor
from simulator.utils.logger import get_logger

logger = get_logger(__name__)


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="MQTT ingestion service for cleaned telemetry storage"
    )
    parser.add_argument(
        "--config",
        default="config/mqtt_config.yaml",
        help="Path to MQTT config YAML",
    )
    args = parser.parse_args()

    config = _load_yaml(Path(args.config))
    ingestor = MQTTIngestor(config)

    logger.info(
        "Starting MQTT ingestion service",
        extra={"event": "ingestion_boot", "config": args.config},
    )
    ingestor.run_forever()


if __name__ == "__main__":
    main()