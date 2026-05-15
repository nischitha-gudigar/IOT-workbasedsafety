"""
run.py
------
Single entry point for the Vitals Simulator.

Usage:
    python run.py \\
      --persona personas/welder_factory.yaml \\
      --scenario scenarios/worker/fatigue_escalation.yaml \\
      [--compression 60] \\
      [--demo]

When --demo is set, the FastAPI demo control UI is started alongside
the simulation engine on DEMO_API_PORT (default 8000).

Signal handling:
    SIGTERM and SIGINT both trigger a graceful shutdown within 5 seconds.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from pathlib import Path

import yaml

from simulator.core.scenario_engine import ScenarioEngine
from simulator.sensors.base import SensorReading
from simulator.transport.mqtt_publisher import MQTTPublisher
from simulator.utils.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def _load_yaml(path: Path) -> dict:
    """Load a YAML config file, returning an empty dict if not found."""
    if not path.exists():
        logger.warning(
            "Config file not found — using defaults",
            extra={"event": "config_not_found", "path": str(path)},
        )
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


# ---------------------------------------------------------------------------
# Publish callback
# ---------------------------------------------------------------------------

def _make_publish_callback(publisher: MQTTPublisher, engine: ScenarioEngine):
    """Create the async publish callback passed to the engine."""
    async def publish(
        reading:    SensorReading,
        persona_id: str,
        poc_type:   str,
        device_id:  str,
    ) -> None:
        status = engine.get_status()
        publisher.publish(
            reading=       reading,
            persona_id=    persona_id,
            poc_type=      poc_type,
            device_id=     device_id,
            sequence_num=  status.sequence_number,
        )
    return publish


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Vitals Simulator — health and safety sensor data simulator"
    )
    parser.add_argument(
        "--persona",
        required=True,
        help="Path to persona YAML file (e.g. personas/welder_factory.yaml)",
    )
    parser.add_argument(
        "--scenario",
        required=True,
        help="Path to scenario YAML file (e.g. scenarios/worker/fatigue_escalation.yaml)",
    )
    parser.add_argument(
        "--compression",
        type=float,
        default=None,
        help="Time compression factor override (overrides scenario YAML value)",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Start the FastAPI demo control UI alongside the simulation",
    )
    args = parser.parse_args()

    # Load config files
    config_dir   = Path("config")
    sim_config   = _load_yaml(config_dir / "simulator_config.yaml")
    mqtt_config  = _load_yaml(config_dir / "mqtt_config.yaml")

    # Build engine
    engine = ScenarioEngine(sim_config=sim_config, mqtt_config=mqtt_config)

    # Load persona and scenario
    engine.load(
        persona_path=  Path(args.persona),
        scenario_path= Path(args.scenario),
    )

    # Override compression if provided
    if args.compression is not None:
        engine.set_compression(args.compression)

    # Connect MQTT publisher
    publisher = MQTTPublisher(mqtt_config)
    publisher.connect()

    # Publish callback
    publish_cb = _make_publish_callback(publisher, engine)

    # Graceful shutdown
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def _handle_signal():
        logger.info("Shutdown signal received", extra={"event": "shutdown_signal"})
        loop.call_soon_threadsafe(shutdown_event.set)

    def _handle_sync_signal(_signum, _frame):
        _handle_signal()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except (NotImplementedError, RuntimeError):
            # Windows' default ProactorEventLoop does not support add_signal_handler.
            signal.signal(sig, _handle_sync_signal)

    logger.info(
        "Simulator starting",
        extra={
            "event":    "simulator_start",
            "persona":  args.persona,
            "scenario": args.scenario,
            "demo":     args.demo,
        },
    )

    # Start engine task
    engine_task = asyncio.create_task(engine.start(publish_cb))

    # Start demo API if requested
    demo_task = None
    if args.demo:
        demo_task = asyncio.create_task(_start_demo_api(engine))

    # With --demo: wait only for SIGTERM/SIGINT — engine reloads via the API must not
    # trigger a process exit. Without --demo: also exit when the scenario completes.
    shutdown_task = asyncio.create_task(shutdown_event.wait())
    wait_tasks = [shutdown_task] if args.demo else [engine_task, shutdown_task]
    done, _ = await asyncio.wait(
        wait_tasks,
        return_when=asyncio.FIRST_COMPLETED,
    )

    # Graceful shutdown
    await engine.stop()
    if demo_task and not demo_task.done():
        demo_task.cancel()
        try:
            await demo_task
        except asyncio.CancelledError:
            pass

    publisher.stop()
    logger.info("Simulator shutdown complete", extra={"event": "simulator_shutdown"})


async def _start_demo_api(engine) -> None:
    """Start the FastAPI demo control UI in an asyncio-compatible server."""
    try:
        import uvicorn
        from demo.demo_api import create_app

        port = int(os.environ.get("DEMO_API_PORT", 8000))
        app  = create_app(engine)

        config = uvicorn.Config(
            app,
            host="0.0.0.0",
            port=port,
            log_level="warning",   # suppress uvicorn access logs
        )
        server = uvicorn.Server(config)

        logger.info(
            "Demo API starting",
            extra={"event": "demo_api_start", "port": port},
        )
        await server.serve()
    except ImportError:
        logger.error(
            "uvicorn not installed — demo API unavailable. "
            "Add uvicorn to requirements.txt.",
            extra={"event": "demo_api_import_error"},
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error(
            "Demo API error",
            extra={"event": "demo_api_error", "error": str(exc)},
        )


if __name__ == "__main__":
    asyncio.run(main())
