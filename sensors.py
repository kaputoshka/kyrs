# sensors.py
import random
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Reading:
    value: float
    unit: str
    timestamp: datetime = field(default_factory=datetime.now)


class _MeanRevertingSensor:
    """Случайное блуждание с возвратом к номинальному значению.

    Без возврата показания со временем уходили бы в крайние значения
    (задержка росла, доступность падала), и система деградировала бы сама
    по себе, независимо от действий оператора.
    """

    NOMINAL = 0.0
    REVERSION = 0.05     # доля отклонения, которая «стягивается» к норме за такт
    NOISE = (-1.0, 1.0)  # шум одного такта
    LIMITS = (0.0, 100.0)
    UNIT = ""

    def __init__(self, seed: int | None = None) -> None:
        self._rng = random.Random(seed)
        self._base = self.NOMINAL

    def read(self) -> Reading:
        self._base += self.REVERSION * (self.NOMINAL - self._base) + self._rng.uniform(*self.NOISE)
        self._base = max(self.LIMITS[0], min(self._base, self.LIMITS[1]))
        return Reading(value=round(self._base, 1), unit=self.UNIT)

    def set_level(self, value: float) -> None:
        """Принудительно задать уровень; дальше показание плавно вернётся к норме."""
        self._base = max(self.LIMITS[0], min(value, self.LIMITS[1]))


class GatewayLatencySensor(_MeanRevertingSensor):
    NOMINAL = 60.0
    NOISE = (-5.0, 5.0)
    LIMITS = (20.0, 400.0)
    UNIT = "мс"


class TrafficSensor(_MeanRevertingSensor):
    NOMINAL = 8.0
    REVERSION = 0.08
    NOISE = (-1.5, 1.5)
    LIMITS = (1.0, 60.0)
    UNIT = "оп/с"


class AvailabilitySensor(_MeanRevertingSensor):
    NOMINAL = 99.0
    REVERSION = 0.15
    NOISE = (-1.2, 0.8)
    LIMITS = (40.0, 100.0)
    UNIT = "%"

    def __init__(self, seed: int | None = None) -> None:
        super().__init__(seed)
        self._failed = False

    def read(self) -> Reading:
        if self._failed:
            # отказ шлюза: показание держится низко, пока шлюз не заменят
            self._base = max(self.LIMITS[0], min(self._base + self._rng.uniform(-1.0, 1.0), 55.0))
            return Reading(value=round(self._base, 1), unit=self.UNIT)
        return super().read()

    def fail(self, level: float = 40.0) -> None:
        """Отказ шлюза: доступность падает и не восстанавливается сама."""
        self._failed = True
        self._base = level

    def restore(self, level: float = 99.0) -> None:
        """Шлюз заменён исправным: показание возвращается к норме."""
        self._failed = False
        self._base = level
