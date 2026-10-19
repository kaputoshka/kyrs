from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

class PaymentStatus(Enum):
        CREATED = "Создан"
        VALIDATING = "Проверка"
        RISK_CHECK = "Риск-проверка"
        ROUTING = "Маршрутизация"
        PROCESSING = "Обработка"
        APPROVED = "Одобрен"
        DECLINED = "Отклонён"
        ERROR = "Ошибка"

@dataclass
class Payment:
        payment_id: str
        amount_rubles: int
        status: PaymentStatus = PaymentStatus.CREATED
        channel: str = "Не выбран"
        terminal_id: str = "—"
        latency_ms: int = 0
        retry_count: int = 0
        created_at: datetime = field(default_factory=datetime.now)
