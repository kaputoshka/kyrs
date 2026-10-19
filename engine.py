import random
import sqlite3
import uuid
from collections import deque
from datetime import datetime

from commands import CommandEvent, CommandType
from incidents import Incident, IncidentSeverity, IncidentStatus, IncidentType
from models import Payment, PaymentStatus
from transitions import ALLOWED_TRANSITIONS, TRANSITION_WEIGHTS
from sensors import GatewayLatencySensor, AvailabilitySensor, TrafficSensor
from storage import get_connection, save_incident, save_payment, save_payments

GATEWAY_FAILURE_THRESHOLD = 60.0   # доступность ниже — авария
GATEWAY_RECOVERY_THRESHOLD = 92.0  # доступность выше — можно закрывать
QUEUE_OVERLOAD_THRESHOLD = 25      # очередь больше — авария
QUEUE_RECOVERY_THRESHOLD = 12      # очередь меньше — можно закрывать
RECOVERY_STREAK_REQUIRED = 4       # столько тактов подряд в норме
LATENCY_FAILURE_THRESHOLD = 180.0  # задержка выше — авария, мс
LATENCY_RECOVERY_THRESHOLD = 110.0 # задержка ниже — восстановление, мс
MAX_RETRIES = 3
MAX_IN_MEMORY_PAYMENTS = 5000

TERMINALS = ["T-1", "T-2", "T-3", "T-4", "T-5", "T-6"]


class SimulationEngine:
    def __init__(self, db: sqlite3.Connection | None = None) -> None:
        self.db = db if db is not None else get_connection()

        self.traffic_history: deque[float] = deque(maxlen=120)
        self.latency_history: deque[float] = deque(maxlen=120)
        self.queue_history: deque[float] = deque(maxlen=120)
        self.active_gateway: str = "Основной"
        self.primary_gateway_failed: bool = False

        self.auto_mode: bool = False
        self._auto_cooldown: int = 0

        self.intensity: float = 1.0  # множитель скорости обработки
        self.blocked_terminals: set[str] = set()
        self.events: list[CommandEvent] = []

        self.incidents: list[Incident] = []
        self._recovery_streak: dict[IncidentType, int] = {}

        self.payments: list[Payment] = []
        self.is_running: bool = False
        self._counter: int = 0

        self.gateway_latency_sensor = GatewayLatencySensor()
        self.availability_sensor = AvailabilitySensor()
        self.traffic_sensor = TrafficSensor()

        self.last_latency_reading = None
        self.last_availability_reading = None
        self.last_traffic_reading = None

    def start(self) -> None:
        self.is_running = True

    def pause(self) -> None:
        self.is_running = False

    def stop(self) -> None:
        """Остановить и полностью сбросить состояние текущего прогона.

        Ранее stop() очищал список аварий, но оставлял датчики и состояние
        шлюза неисправными. После повторного старта авария возникала снова,
        а основной шлюз мог остаться недоступным без возможности ремонта.
        """
        self.is_running = False
        self.payments.clear()
        self._counter = 0
        self.incidents.clear()
        self._recovery_streak.clear()
        self._auto_cooldown = 0
        self.traffic_history.clear()
        self.latency_history.clear()
        self.queue_history.clear()
        self.last_latency_reading = None
        self.last_availability_reading = None
        self.last_traffic_reading = None

        # Сбросить именно физическое состояние симуляции, а не только UI.
        self.active_gateway = "Основной"
        self.primary_gateway_failed = False
        self.intensity = 1.0
        self.blocked_terminals.clear()
        self.gateway_latency_sensor.set_level(self.gateway_latency_sensor.NOMINAL)
        self.availability_sensor.restore(self.availability_sensor.NOMINAL)
        self.traffic_sensor.set_level(self.traffic_sensor.NOMINAL)

    def tick(self) -> list[Payment]:
        if not self.is_running:
            return []

        self.last_latency_reading = self.gateway_latency_sensor.read()
        self.last_availability_reading = self.availability_sensor.read()
        self.last_traffic_reading = self.traffic_sensor.read()

        changed: list[Payment] = []
        # Платёж, созданный в текущем такте, начинает обработку со следующего
        # такта: это исключает скрытое ускорение жизненного цикла при высокой нагрузке.
        existing_ids = {payment.payment_id for payment in self.payments}
        spawn_chance = min(self.last_traffic_reading.value / 20, 0.9)
        if random.random() < spawn_chance:
            changed.append(self._spawn_payment())

        for payment in self.payments:
            if payment.payment_id not in existing_ids:
                continue
            if random.random() < min(0.7 * self.intensity, 1.0) and self._advance(payment):
                changed.append(payment)
        self.traffic_history.append(self.last_traffic_reading.value)
        self.latency_history.append(self.last_latency_reading.value)
        unfinished = sum(
            1 for p in self.payments
            if p.status not in (PaymentStatus.APPROVED, PaymentStatus.DECLINED, PaymentStatus.ERROR)
        )
        self.queue_history.append(unfinished)
        # Сначала фиксируем аварию по показаниям такта, и только потом даём
        # автоправилам на неё отреагировать. Иначе в автоматическом режиме
        # правило успевает переключить шлюз раньше, чем авария будет замечена.
        self.evaluate_incidents()
        self.apply_auto_rules()

        # Один атомарный commit на такт вместо commit на каждый платеж.
        # Убираем повторяющиеся объекты, если платёж был изменён более одного раза.
        changed_by_id = {payment.payment_id: payment for payment in changed}
        save_payments(self.db, changed_by_id.values())

        for incident in self.incidents:
            if incident.status == IncidentStatus.RESOLVED and not incident.saved:
                save_incident(self.db, incident)
                incident.saved = True
        return changed

    def _spawn_payment(self) -> Payment:
        self._counter += 1
        payment = Payment(
            payment_id=f"TX-{self._counter:03d}-{uuid.uuid4().hex[:4].upper()}",
            amount_rubles=random.randint(150, 25000),
            terminal_id=random.choice(TERMINALS),
        )
        self.payments.append(payment)
        # Ограничиваем память при длительной демонстрации; полная история остаётся в SQLite.
        if len(self.payments) > MAX_IN_MEMORY_PAYMENTS:
            del self.payments[:len(self.payments) - MAX_IN_MEMORY_PAYMENTS]
        return payment

    def _advance(self, payment: Payment) -> bool:
        possible = ALLOWED_TRANSITIONS[payment.status]
        if not possible:
            return False

        if payment.status == PaymentStatus.RISK_CHECK and payment.terminal_id in self.blocked_terminals:
            # блокировка терминала: платёж отклоняется на риск-проверке
            payment.status = PaymentStatus.DECLINED
        elif payment.status == PaymentStatus.ROUTING and self.last_availability_reading.value < 90:
            # активный шлюз недоступен — маршрутизация завершается ошибкой
            payment.channel = self.active_gateway
            payment.status = PaymentStatus.ERROR
        else:
            weights = TRANSITION_WEIGHTS.get(payment.status)
            if weights:
                payment.status = random.choices(list(weights), weights=list(weights.values()))[0]
            else:
                payment.status = possible[0]
            if payment.status == PaymentStatus.PROCESSING:
                # маршрутизация выбрала канал: платёж уходит через активный шлюз
                payment.channel = self.active_gateway

        payment.latency_ms += int(self.last_latency_reading.value / 3)
        return True

    def switch_gateway(self, target: str) -> CommandEvent:
        valid_target = target in ("Основной", "Резервный")
        success = valid_target and target != self.active_gateway
        details = ""
        if success and target == "Основной" and (self.availability_sensor._failed or self.primary_gateway_failed):
            success = False
            details = "основной шлюз неисправен; сначала восстановите его"
        if success:
            self.active_gateway = target
            if target == "Резервный":
                # Запоминаем отказ основного шлюза отдельно: датчик далее отражает
                # доступность текущего маршрута и восстанавливается для резервного.
                self.primary_gateway_failed = self.primary_gateway_failed or self.availability_sensor._failed
                self.availability_sensor.restore(99.0)
            details = f"активен {self.active_gateway}"
        elif not details:
            details = f"активен {self.active_gateway}" if valid_target else f"неизвестный шлюз: {target}"

        event = CommandEvent(
            CommandType.SWITCH_GATEWAY,
            details,
            success,
        )
        self.events.append(event)
        return event

    def restore_primary_gateway(self) -> CommandEvent:
        """Смоделировать ремонт основного шлюза, не меняя текущий маршрут."""
        was_failed = self.primary_gateway_failed or self.availability_sensor._failed
        self.primary_gateway_failed = False
        self.availability_sensor.restore(self.availability_sensor.NOMINAL)
        details = ("основной шлюз восстановлен; маршрут не изменён"
                   if was_failed else "основной шлюз уже исправен")
        event = CommandEvent(CommandType.SWITCH_GATEWAY, details, was_failed)
        self.events.append(event)
        return event

    def set_intensity(self, value: float) -> CommandEvent:
        success = 0.2 <= value <= 3.0
        if success:
            self.intensity = value
        event = CommandEvent(
            CommandType.SET_INTENSITY,
            f"x{self.intensity:.1f}",
            success,
        )
        self.events.append(event)
        return event

    def retry_processing(self) -> CommandEvent:
        # Повторяем только временные ошибки, не создавая бесконечный цикл ретраев.
        # При недоступном шлюзе повтор лишь увеличил бы число ошибок.
        available = self.last_availability_reading.value if self.last_availability_reading else 99.0
        candidates = [p for p in self.payments if p.status == PaymentStatus.ERROR]
        if available < GATEWAY_FAILURE_THRESHOLD or (self.active_gateway == "Основной" and self.primary_gateway_failed):
            retried = []
            details = "повтор невозможен: шлюз недоступен"
        else:
            retried = [p for p in candidates if p.retry_count < MAX_RETRIES]
            for payment in retried:
                payment.retry_count += 1
                payment.status = PaymentStatus.ROUTING
                payment.channel = "Не выбран"
                payment.latency_ms = 0
            save_payments(self.db, retried)
            exhausted = len(candidates) - len(retried)
            details = f"возвращено в обработку: {len(retried)}"
            if exhausted:
                details += f", исчерпан лимит повторов: {exhausted}"
        event = CommandEvent(
            CommandType.RETRY_PROCESSING,
            details,
            bool(retried),
        )
        self.events.append(event)
        return event

    def block_terminal(self, terminal_id: str) -> CommandEvent:
        terminal_id = terminal_id.strip().upper()
        if terminal_id not in TERMINALS:
            details = f"{terminal_id or '—'}: неизвестный терминал"
            success = False
        elif terminal_id in self.blocked_terminals:
            details = f"{terminal_id}: уже заблокирован"
            success = False
        else:
            self.blocked_terminals.add(terminal_id)
            details = terminal_id
            success = True
        event = CommandEvent(CommandType.BLOCK_TERMINAL, details, success)
        self.events.append(event)
        return event

    def unblock_terminal(self, terminal_id: str) -> CommandEvent:
        terminal_id = terminal_id.strip().upper()
        success = terminal_id in self.blocked_terminals
        if success:
            self.blocked_terminals.discard(terminal_id)
        details = terminal_id if success else f"{terminal_id or '—'}: не был заблокирован"
        event = CommandEvent(CommandType.UNBLOCK_TERMINAL, details, success)
        self.events.append(event)
        return event

    def apply_auto_rules(self) -> None:
        if not self.auto_mode:
            return

        if self._auto_cooldown > 0:
            self._auto_cooldown -= 1
            return

        gateway_unavailable = (
            self.last_availability_reading is not None
            and self.last_availability_reading.value < 90
        )
        latency_critical = (
            self.last_latency_reading is not None
            and self.last_latency_reading.value >= LATENCY_FAILURE_THRESHOLD
        )
        if (gateway_unavailable or latency_critical) and self.active_gateway != "Резервный":
            event = self.switch_gateway("Резервный")
            if latency_critical and not gateway_unavailable:
                # При переключении на резервный шлюз моделируем меньшую задержку маршрута.
                self.gateway_latency_sensor.set_level(95.0)
                event.details = "авто: высокая задержка, включён резервный шлюз"
            else:
                event.details = f"авто: {event.details}"
            self._auto_cooldown = 5

        queue_length = self.queue_history[-1] if self.queue_history else 0
        # второе условие: пока идёт авария перегрузки, интенсивность должна
        # дойти хотя бы до порога восстановления (1.5), иначе авария не закроется
        overload_active = self._find_active(IncidentType.QUEUE_OVERLOAD) is not None
        if (queue_length > 15 and self.intensity < 2.5) or (overload_active and self.intensity < 1.5):
            event = self.set_intensity(min(self.intensity + 0.3, 3.0))
            event.details = f"авто: {event.details}"
            self._auto_cooldown = 3

    def inject_traffic_burst(self, count: int = 30) -> None:
        """Демонстрация аварии №2: резкий всплеск входящих платежей."""
        self.traffic_sensor.set_level(30.0)
        for _ in range(count):
            self._spawn_payment()

    def inject_latency_spike(self, level: float = 320.0) -> None:
        """Демонстрация аварии: кратковременный скачок задержки шлюза."""
        self.gateway_latency_sensor.set_level(max(200.0, min(float(level), 400.0)))

    # ---------- аварии ----------

    def evaluate_incidents(self) -> None:
        self._evaluate_gateway_failure()
        self._evaluate_queue_overload()
        self._evaluate_high_latency()

    def _evaluate_gateway_failure(self) -> None:
        active = self._find_active(IncidentType.GATEWAY_FAILURE)
        availability = self.last_availability_reading.value

        if active is None:
            if availability < GATEWAY_FAILURE_THRESHOLD and self.active_gateway == "Основной":
                incident = Incident(
                    incident_type=IncidentType.GATEWAY_FAILURE,
                    severity=IncidentSeverity.CRITICAL,
                    cause=f"доступность упала до {availability:.1f}%",
                )
                incident.add_log(f"Авария начата: {incident.cause}")
                self.incidents.append(incident)
            return

        self._progress_incident(
            active,
            recovered_condition=(
                availability >= GATEWAY_RECOVERY_THRESHOLD
                and self.active_gateway == "Резервный"
            ),
        )

    def _evaluate_queue_overload(self) -> None:
        active = self._find_active(IncidentType.QUEUE_OVERLOAD)
        queue_length = int(self.queue_history[-1]) if self.queue_history else 0

        if active is None:
            if queue_length > QUEUE_OVERLOAD_THRESHOLD:
                incident = Incident(
                    incident_type=IncidentType.QUEUE_OVERLOAD,
                    severity=IncidentSeverity.CRITICAL,
                    cause=f"очередь выросла до {queue_length} платежей",
                )
                incident.add_log(f"Авария начата: {incident.cause}")
                self.incidents.append(incident)
            return

        self._progress_incident(
            active,
            recovered_condition=(
                queue_length < QUEUE_RECOVERY_THRESHOLD and self.intensity >= 1.5
            ),
        )

    def _evaluate_high_latency(self) -> None:
        """Обнаруживает длительный рост задержки и закрывает аварию с гистерезисом."""
        active = self._find_active(IncidentType.HIGH_LATENCY)
        latency = self.last_latency_reading.value if self.last_latency_reading else 0.0

        if active is None:
            if latency >= LATENCY_FAILURE_THRESHOLD:
                incident = Incident(
                    incident_type=IncidentType.HIGH_LATENCY,
                    severity=IncidentSeverity.CRITICAL,
                    cause=f"задержка выросла до {latency:.0f} мс",
                )
                incident.add_log(f"Авария начата: {incident.cause}")
                self.incidents.append(incident)
            return

        self._progress_incident(
            active,
            recovered_condition=latency <= LATENCY_RECOVERY_THRESHOLD,
        )

    def _find_active(self, incident_type: IncidentType) -> Incident | None:
        for incident in self.incidents:
            if incident.incident_type == incident_type and incident.status != IncidentStatus.RESOLVED:
                return incident
        return None

    def _progress_incident(self, incident: Incident, recovered_condition: bool) -> None:
        key = incident.incident_type

        if not recovered_condition:
            self._recovery_streak[key] = 0
            if incident.status != IncidentStatus.ACTIVE:
                incident.status = IncidentStatus.ACTIVE
                incident.add_log("Показатели снова ухудшились, восстановление прервано")
            return

        was_recovering = incident.status == IncidentStatus.RECOVERING
        incident.status = IncidentStatus.RECOVERING
        self._recovery_streak[key] = self._recovery_streak.get(key, 0) + 1
        if not was_recovering:
            incident.add_log("Показатели нормализовались, начат контроль восстановления")

        if self._recovery_streak[key] >= RECOVERY_STREAK_REQUIRED:
            incident.status = IncidentStatus.RESOLVED
            incident.resolved_at = datetime.now()
            incident.add_log("Авария устранена")
            self._recovery_streak[key] = 0
