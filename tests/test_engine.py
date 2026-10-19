import unittest

from engine import RECOVERY_STREAK_REQUIRED, SimulationEngine
from models import PaymentStatus
from incidents import IncidentStatus, IncidentType
from storage import get_connection


def make_engine() -> SimulationEngine:
    # база в памяти: тесты не должны писать в настоящий payflow.db
    return SimulationEngine(db=get_connection(":memory:"))


class EngineTestCase(unittest.TestCase):
    def test_stop_clears_payments(self) -> None:
        engine = make_engine()
        engine.start()
        for _ in range(20):
            engine.tick()

        engine.stop()

        self.assertEqual(engine.payments, [])
        self.assertFalse(engine.is_running)

    def test_tick_does_nothing_when_not_running(self) -> None:
        engine = make_engine()
        changed = engine.tick()
        self.assertEqual(changed, [])

    def test_stop_resets_faults_and_gateway_state(self) -> None:
        engine = make_engine()
        engine.start()
        engine.availability_sensor.fail(40.0)
        engine.gateway_latency_sensor.set_level(320.0)
        engine.traffic_sensor.set_level(50.0)
        engine.primary_gateway_failed = True
        engine.active_gateway = "Резервный"
        engine.blocked_terminals.add("T-1")
        engine.intensity = 2.4

        engine.stop()

        self.assertFalse(engine.is_running)
        self.assertEqual(engine.active_gateway, "Основной")
        self.assertFalse(engine.primary_gateway_failed)
        self.assertFalse(engine.availability_sensor._failed)
        self.assertGreaterEqual(engine.availability_sensor._base, 92.0)
        self.assertAlmostEqual(engine.gateway_latency_sensor._base, 60.0)
        self.assertAlmostEqual(engine.traffic_sensor._base, 8.0)
        self.assertEqual(engine.blocked_terminals, set())
        self.assertEqual(engine.intensity, 1.0)


class IncidentsTestCase(unittest.TestCase):
    def test_gateway_failure_is_detected_from_sensor(self) -> None:
        engine = make_engine()
        engine.start()
        engine.availability_sensor.fail(40.0)
        engine.tick()

        incident = engine.incidents[0]
        self.assertEqual(incident.incident_type, IncidentType.GATEWAY_FAILURE)
        self.assertEqual(incident.status, IncidentStatus.ACTIVE)

    def test_incident_is_not_closed_instantly(self) -> None:
        engine = make_engine()
        engine.start()
        engine.availability_sensor.fail(40.0)
        engine.tick()

        engine.switch_gateway("Резервный")  # ручное восстановление
        for _ in range(RECOVERY_STREAK_REQUIRED - 1):
            engine.tick()
        self.assertEqual(engine.incidents[0].status, IncidentStatus.RECOVERING)

        engine.tick()  # последний такт серии
        self.assertEqual(engine.incidents[0].status, IncidentStatus.RESOLVED)
        self.assertEqual(engine.incidents[0].log[-1].split("  ", 1)[1], "Авария устранена")

    def test_auto_mode_recovers_gateway_failure(self) -> None:
        engine = make_engine()
        engine.auto_mode = True
        engine.start()
        engine.availability_sensor.fail(40.0)
        for _ in range(RECOVERY_STREAK_REQUIRED + 3):
            engine.tick()

        self.assertEqual(engine.active_gateway, "Резервный")
        self.assertEqual(engine.incidents[0].status, IncidentStatus.RESOLVED)

    def test_high_latency_incident_is_detected(self) -> None:
        engine = make_engine()
        engine.start()
        engine.gateway_latency_sensor.set_level(320.0)
        engine.tick()
        incidents = [i for i in engine.incidents if i.incident_type == IncidentType.HIGH_LATENCY]
        self.assertTrue(incidents)
        self.assertEqual(incidents[0].status, IncidentStatus.ACTIVE)

    def test_auto_mode_fails_over_on_critical_latency(self) -> None:
        engine = make_engine()
        engine.auto_mode = True
        engine.start()
        engine.gateway_latency_sensor.set_level(320.0)
        engine.tick()
        self.assertEqual(engine.active_gateway, "Резервный")
        incident = next(i for i in engine.incidents if i.incident_type == IncidentType.HIGH_LATENCY)
        for _ in range(RECOVERY_STREAK_REQUIRED):
            engine.tick()
        self.assertEqual(incident.status, IncidentStatus.RESOLVED)

    def test_high_latency_incident_recovers_after_latency_normalizes(self) -> None:
        engine = make_engine()
        engine.start()
        engine.gateway_latency_sensor.set_level(320.0)
        engine.tick()
        incident = next(i for i in engine.incidents if i.incident_type == IncidentType.HIGH_LATENCY)
        engine.gateway_latency_sensor.set_level(80.0)
        for _ in range(RECOVERY_STREAK_REQUIRED):
            engine.tick()
        self.assertEqual(incident.status, IncidentStatus.RESOLVED)

    def test_queue_overload_recovers_automatically(self) -> None:
        engine = make_engine()
        engine.auto_mode = True
        engine.start()
        engine.inject_traffic_burst(40)
        for _ in range(120):
            engine.tick()
            if engine.incidents and engine.incidents[0].status == IncidentStatus.RESOLVED:
                break

        self.assertEqual(engine.incidents[0].incident_type, IncidentType.QUEUE_OVERLOAD)
        self.assertEqual(engine.incidents[0].status, IncidentStatus.RESOLVED)

    def test_resolved_incident_saved_exactly_once(self) -> None:
        engine = make_engine()
        engine.start()
        engine.availability_sensor.fail(40.0)
        engine.tick()
        engine.switch_gateway("Резервный")
        for _ in range(60):
            engine.tick()
            if engine.incidents[0].status == IncidentStatus.RESOLVED:
                break
        for _ in range(5):  # лишние такты не должны дублировать запись
            engine.tick()

        count = engine.db.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]
        self.assertEqual(count, 1)


class CommandsAffectModelTestCase(unittest.TestCase):
    """Правило приёмки: команда действительно меняет поведение модели."""

    def _finish_all(self, engine: SimulationEngine) -> None:
        engine.intensity = 3.0  # вероятность шага >= 1: платежи идут без пауз
        for _ in range(150):  # с запасом: каждый из 6 терминалов получит платежи
            engine.tick()

    def test_blocked_terminal_payments_are_declined(self) -> None:
        engine = make_engine()
        engine.start()
        engine.traffic_sensor.set_level(30.0)
        engine.block_terminal("T-1")
        self._finish_all(engine)

        t1 = [p for p in engine.payments if p.terminal_id == "T-1"]
        self.assertTrue(t1)
        # дальше риск-проверки платежи заблокированного терминала не проходят
        allowed = {PaymentStatus.CREATED, PaymentStatus.VALIDATING,
                   PaymentStatus.RISK_CHECK, PaymentStatus.DECLINED}
        self.assertTrue(all(p.status in allowed for p in t1))
        self.assertTrue(any(p.status == PaymentStatus.DECLINED for p in t1))

        other = [p for p in engine.payments if p.terminal_id != "T-1"]
        self.assertTrue(any(p.status == PaymentStatus.APPROVED for p in other))

    def test_unblock_restores_terminal(self) -> None:
        engine = make_engine()
        engine.block_terminal("T-2")
        self.assertTrue(engine.unblock_terminal("T-2").success)
        self.assertEqual(engine.blocked_terminals, set())

    def test_unknown_terminal_is_rejected(self) -> None:
        engine = make_engine()
        self.assertFalse(engine.block_terminal("X-99").success)
        self.assertFalse(engine.block_terminal("").success)

    def test_channel_follows_active_gateway(self) -> None:
        engine = make_engine()
        engine.start()
        engine.switch_gateway("Резервный")
        engine.traffic_sensor.set_level(30.0)
        self._finish_all(engine)

        routed = [p for p in engine.payments if p.channel != "Не выбран"]
        self.assertTrue(routed)
        self.assertTrue(all(p.channel == "Резервный" for p in routed))

    def test_retry_processing_resets_attempt_and_increments_counter(self) -> None:
        from models import Payment
        from datetime import datetime
        engine = make_engine()
        payment = Payment("TX-RETRY", 500, status=PaymentStatus.ERROR,
                          channel="Основной", latency_ms=250)
        engine.payments.append(payment)
        event = engine.retry_processing()
        self.assertTrue(event.success)
        self.assertEqual(payment.status, PaymentStatus.ROUTING)
        self.assertEqual(payment.retry_count, 1)
        self.assertEqual(payment.channel, "Не выбран")
        self.assertEqual(payment.latency_ms, 0)
        row = engine.db.execute("SELECT retry_count, status FROM payments WHERE payment_id=?",
                                (payment.payment_id,)).fetchone()
        self.assertEqual(row, (1, "Маршрутизация"))

    def test_retry_limit_prevents_infinite_retries(self) -> None:
        from models import Payment
        engine = make_engine()
        payment = Payment("TX-LIMIT", 500, status=PaymentStatus.ERROR, retry_count=3)
        engine.payments.append(payment)
        event = engine.retry_processing()
        self.assertFalse(event.success)
        self.assertEqual(payment.status, PaymentStatus.ERROR)
        self.assertEqual(payment.retry_count, 3)
        self.assertIn("исчерпан лимит", event.details)

    def test_primary_gateway_cannot_be_selected_while_failed(self) -> None:
        engine = make_engine()
        engine.availability_sensor.fail(40)
        event = engine.switch_gateway("Резервный")
        self.assertTrue(event.success)
        event = engine.switch_gateway("Основной")
        self.assertFalse(event.success)
        self.assertEqual(engine.active_gateway, "Резервный")

    def test_new_payment_has_no_channel_until_routing(self) -> None:
        engine = make_engine()
        payment = engine._spawn_payment()
        self.assertEqual(payment.channel, "Не выбран")

    def test_most_payments_are_approved_in_normal_mode(self) -> None:
        engine = make_engine()
        engine.start()
        engine.intensity = 3.0
        engine.traffic_sensor.set_level(30.0)
        for _ in range(200):
            engine.tick()
        done = [p for p in engine.payments if p.status in (
            PaymentStatus.APPROVED, PaymentStatus.DECLINED, PaymentStatus.ERROR)]
        approved = [p for p in done if p.status == PaymentStatus.APPROVED]
        self.assertGreater(len(approved) / len(done), 0.6)


class SensorsTestCase(unittest.TestCase):
    def test_sensors_stay_near_nominal_over_long_run(self) -> None:
        from sensors import AvailabilitySensor, GatewayLatencySensor, TrafficSensor

        for sensor_class, low, high in (
            (GatewayLatencySensor, 20, 140),
            (TrafficSensor, 1, 25),
            (AvailabilitySensor, 90, 100),
        ):
            sensor = sensor_class(seed=1)
            values = [sensor.read().value for _ in range(5000)]
            self.assertTrue(low <= min(values) and max(values) <= high, sensor_class.__name__)

    def test_primary_gateway_can_be_repaired_after_failover(self) -> None:
        engine = make_engine()
        engine.start()
        engine.availability_sensor.fail(40.0)
        engine.tick()
        engine.switch_gateway("Резервный")
        self.assertTrue(engine.primary_gateway_failed)

        event = engine.restore_primary_gateway()

        self.assertTrue(event.success)
        self.assertFalse(engine.primary_gateway_failed)
        self.assertFalse(engine.availability_sensor._failed)
        # Ремонт компонента не должен самовольно менять активный маршрут.
        self.assertEqual(engine.active_gateway, "Резервный")

    def test_gateway_failure_persists_until_restored(self) -> None:
        from sensors import AvailabilitySensor

        sensor = AvailabilitySensor(seed=1)
        sensor.fail(40.0)
        self.assertTrue(all(sensor.read().value < 60 for _ in range(500)))
        sensor.restore(99.0)
        self.assertGreater(sensor.read().value, 90)

    def test_traffic_burst_fades_back_to_normal(self) -> None:
        from sensors import TrafficSensor

        sensor = TrafficSensor(seed=1)
        sensor.set_level(30.0)
        values = [sensor.read().value for _ in range(100)]
        self.assertGreater(values[0], 20)
        self.assertLess(values[-1], 16)


class PersistenceTestCase(unittest.TestCase):
    def test_payments_are_written_to_db(self) -> None:
        engine = make_engine()
        engine.start()
        for _ in range(30):
            engine.tick()

        count = engine.db.execute("SELECT COUNT(*) FROM payments").fetchone()[0]
        self.assertEqual(count, len(engine.payments))

    def test_old_database_gets_terminal_column(self) -> None:
        import sqlite3
        import tempfile
        from pathlib import Path

        path = Path(tempfile.mkdtemp()) / "old.db"
        old = sqlite3.connect(path)
        old.execute("CREATE TABLE payments (payment_id TEXT PRIMARY KEY, amount_rubles INTEGER NOT NULL, "
                    "status TEXT NOT NULL, channel TEXT NOT NULL, latency_ms INTEGER NOT NULL, created_at TEXT NOT NULL)")
        old.execute("INSERT INTO payments VALUES ('TX-1', 100, 'Одобрен', 'Основной', 5, '2026-01-01T10:00:00')")
        old.commit()
        old.close()

        connection = get_connection(path)
        row = connection.execute("SELECT terminal_id, retry_count FROM payments").fetchone()
        self.assertEqual(row, ("—", 0))


if __name__ == "__main__":
    unittest.main()


class FinancialIntegrityTestCase(unittest.TestCase):
    def test_batch_save_is_atomic_when_one_payment_is_invalid(self) -> None:
        from models import Payment
        from storage import save_payments

        engine = make_engine()
        valid = Payment("TX-ATOMIC-1", 1000)
        invalid = Payment("TX-ATOMIC-2", 0)
        with self.assertRaises(ValueError):
            save_payments(engine.db, [valid, invalid])
        count = engine.db.execute("SELECT COUNT(*) FROM payments").fetchone()[0]
        self.assertEqual(count, 0)

    def test_payment_id_is_idempotent_and_updates_state_not_duplicate_row(self) -> None:
        from models import Payment
        from storage import save_payment

        engine = make_engine()
        payment = Payment("TX-IDEMPOTENT", 1250)
        save_payment(engine.db, payment)
        payment.status = PaymentStatus.APPROVED
        payment.channel = "Основной"
        save_payment(engine.db, payment)
        count = engine.db.execute(
            "SELECT COUNT(*) FROM payments WHERE payment_id=?", (payment.payment_id,)
        ).fetchone()[0]
        row = engine.db.execute(
            "SELECT status, channel FROM payments WHERE payment_id=?", (payment.payment_id,)
        ).fetchone()
        self.assertEqual(count, 1)
        self.assertEqual(row, (PaymentStatus.APPROVED.value, "Основной"))

    def test_statistics_cover_all_persisted_payments(self) -> None:
        from models import Payment
        from storage import get_payment_statistics, save_payments

        engine = make_engine()
        save_payments(engine.db, [
            Payment("TX-STAT-1", 100, status=PaymentStatus.APPROVED),
            Payment("TX-STAT-2", 200, status=PaymentStatus.ERROR, retry_count=2),
            Payment("TX-STAT-3", 300, status=PaymentStatus.PROCESSING),
        ])
        stats = get_payment_statistics(engine.db)
        self.assertEqual(stats["total"], 3)
        self.assertEqual(stats["amount_rubles"], 600)
        self.assertEqual(stats["approved"], 1)
        self.assertEqual(stats["errors"], 1)
        self.assertEqual(stats["in_progress"], 1)
        self.assertEqual(stats["retries"], 2)

    def test_new_payment_does_not_advance_in_creation_tick(self) -> None:
        from unittest.mock import patch
        engine = make_engine()
        engine.start()
        engine.traffic_sensor.set_level(30.0)
        # Первый random вызывает создание, остальные не должны продвигать новую запись.
        with patch("engine.random.random", side_effect=[0.0]):
            changed = engine.tick()
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0].status, PaymentStatus.CREATED)
