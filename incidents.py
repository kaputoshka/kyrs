# incidents.py
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class IncidentSeverity(Enum):
    WARNING = "Предупреждение"
    CRITICAL = "Критическая"


class IncidentStatus(Enum):
    ACTIVE = "Активна"
    RECOVERING = "Восстановление"
    RESOLVED = "Устранена"


class IncidentType(Enum):
    GATEWAY_FAILURE = "Отказ основного шлюза"
    QUEUE_OVERLOAD = "Перегрузка очереди"
    HIGH_LATENCY = "Критический рост задержки"


@dataclass
class Incident:
    incident_type: IncidentType
    severity: IncidentSeverity
    cause: str
    status: IncidentStatus = IncidentStatus.ACTIVE
    started_at: datetime = field(default_factory=datetime.now)
    resolved_at: datetime | None = None
    log: list[str] = field(default_factory=list)
    saved: bool = False  # записана ли уже в базу (занятие №12)

    def add_log(self, text: str) -> None:
        time_str = datetime.now().strftime("%H:%M:%S")
        self.log.append(f"{time_str}  {text}")
