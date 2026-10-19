# commands.py
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class CommandType(Enum):
    SWITCH_GATEWAY = "Переключение шлюза"
    SET_INTENSITY = "Изменение интенсивности"
    RETRY_PROCESSING = "Повтор обработки"
    BLOCK_TERMINAL = "Блокировка терминала"
    UNBLOCK_TERMINAL = "Разблокировка терминала"


@dataclass
class CommandEvent:
    command_type: CommandType
    details: str
    success: bool
    timestamp: datetime = field(default_factory=datetime.now)

    def as_log_line(self) -> str:
        mark = "✓" if self.success else "✗"
        time_str = self.timestamp.strftime("%H:%M:%S")
        return f"{time_str}  {mark}  {self.command_type.value}: {self.details}"

