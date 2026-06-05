"""YAML config loader for rotator-remote. Trimmed from spe-remote/spe/config.py."""

import yaml
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class SerialConfig:
    # Stable by-id path for the Rotor-EZ FTDI cable. 4800-8N1 is the
    # Rotor-EZ default and is NOT configurable on the controller.
    port: str = "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL05J29R-if00-port0"
    baudrate: int = 4800
    timeout: float = 0.2          # blocking-read timeout for the reader thread


@dataclass
class ServerConfig:
    port: int = 8090              # spe=8888, lp700=8089, rotator=8090
    host: str = "0.0.0.0"


@dataclass
class PollingConfig:
    poll_interval: float = 1.0           # seconds between AI1; azimuth queries
    heartbeat: float = 15.0              # force a state re-broadcast every N s
    presence_heartbeat: float = 5.0      # presence/serial-status heartbeat every N s
    serial_alive_threshold: float = 5.0  # reply within N s ⇒ serial considered "up"


@dataclass
class AppConfig:
    serial: SerialConfig = field(default_factory=SerialConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    polling: PollingConfig = field(default_factory=PollingConfig)
    log_level: str = "INFO"


def load_config(path: str = "config.yaml") -> AppConfig:
    config = AppConfig()
    config_path = Path(path)

    if config_path.exists():
        with open(config_path) as f:
            raw = yaml.safe_load(f) or {}

        for section, target in (
            ("serial", config.serial),
            ("server", config.server),
            ("polling", config.polling),
        ):
            if section in raw and isinstance(raw[section], dict):
                for k, v in raw[section].items():
                    if hasattr(target, k):
                        setattr(target, k, v)

        if "logging" in raw and isinstance(raw["logging"], dict):
            config.log_level = raw["logging"].get("level", "INFO")

        logger.info(f"Loaded config from {config_path}")
    else:
        logger.warning(f"Config file {config_path} not found, using defaults")

    return config
