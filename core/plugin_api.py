# core/plugin_api.py
from dataclasses import dataclass

@dataclass
class WeaponConfig:
    name: str
    defaultPull: float = 2.0
    initialDuration: float = 0.5
    steadyPull: float = 1.6
    sleepTime: int = 8
    acceleration: float = 200.0
