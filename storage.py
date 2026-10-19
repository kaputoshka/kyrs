# storage.py
import sqlite3
import sys
from pathlib import Path


def _default_db_path() -> Path:
    # В собранном .exe (PyInstaller --onefile) __file__ указывает во временную
    # папку, которая удаляется при выходе. База должна лежать рядом с .exe.
    if getattr(sys, "frozen", False):
        return Path(sys.executable).with_name("payflow.db")
    return Path(__file__).with_name("payflow.db")


DB_PATH = _default_db_path()

SCHEMA = """
CREATE TABLE IF NOT EXISTS payments (
    payment_id TEXT PRIMARY KEY,
    amount_rubles INTEGER NOT NULL,
    status TEXT NOT NULL,
    channel TEXT NOT NULL,
    latency_ms INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    terminal_id TEXT NOT NULL DEFAULT '—',
    retry_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    cause TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_payments_created_at ON payments(created_at);
CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status);
CREATE INDEX IF NOT EXISTS idx_incidents_started_at ON incidents(started_at);
"""


def get_connection(path: str | Path | None = None) -> sqlite3.Connection:
    """path=":memory:" нужен тестам, чтобы не трогать настоящую базу."""
    connection = sqlite3.connect(DB_PATH if path is None else path)
    connection.executescript(SCHEMA)
    _migrate(connection)
    return connection


def _migrate(connection: sqlite3.Connection) -> None:
    """Старые базы (до добавления терминала) дополняются недостающим столбцом."""
    columns = {row[1] for row in connection.execute("PRAGMA table_info(payments)")}
    if "terminal_id" not in columns:
        connection.execute("ALTER TABLE payments ADD COLUMN terminal_id TEXT NOT NULL DEFAULT '—'")
    if "retry_count" not in columns:
        connection.execute("ALTER TABLE payments ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0")
    connection.commit()


def _payment_values(payment) -> tuple:
    if not payment.payment_id or not str(payment.payment_id).strip():
        raise ValueError("payment_id не может быть пустым")
    if not isinstance(payment.amount_rubles, int) or payment.amount_rubles <= 0:
        raise ValueError("сумма платежа должна быть положительным целым числом")
    if payment.retry_count < 0:
        raise ValueError("счётчик повторов не может быть отрицательным")
    return (
        payment.payment_id,
        payment.amount_rubles,
        payment.status.value,
        payment.channel,
        payment.latency_ms,
        payment.created_at.isoformat(),
        payment.terminal_id,
        payment.retry_count,
    )


_UPSERT_PAYMENT = """
INSERT INTO payments (payment_id, amount_rubles, status, channel, latency_ms, created_at, terminal_id, retry_count)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(payment_id) DO UPDATE SET
    status = excluded.status,
    channel = excluded.channel,
    latency_ms = excluded.latency_ms,
    retry_count = excluded.retry_count
"""


def save_payments(connection: sqlite3.Connection, payments) -> None:
    """Сохраняет набор платежей атомарно: либо все изменения, либо ни одного."""
    values = [_payment_values(payment) for payment in payments]
    if not values:
        return
    try:
        connection.execute("BEGIN")
        connection.executemany(_UPSERT_PAYMENT, values)
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def save_payment(connection: sqlite3.Connection, payment) -> None:
    save_payments(connection, [payment])


def get_payment_statistics(connection: sqlite3.Connection) -> dict[str, int]:
    """Агрегированная статистика по всей истории, а не только по строкам в памяти."""
    rows = connection.execute(
        "SELECT status, COUNT(*), COALESCE(SUM(amount_rubles), 0) FROM payments GROUP BY status"
    ).fetchall()
    result = {"total": 0, "amount_rubles": 0, "approved": 0, "declined": 0,
              "errors": 0, "in_progress": 0, "retries": 0}
    final_statuses = {"Одобрен", "Отклонён", "Ошибка"}
    for status, count, amount in rows:
        result["total"] += count
        result["amount_rubles"] += amount
        if status == "Одобрен":
            result["approved"] += count
        elif status == "Отклонён":
            result["declined"] += count
        elif status == "Ошибка":
            result["errors"] += count
        if status not in final_statuses:
            result["in_progress"] += count
    retry_row = connection.execute("SELECT COALESCE(SUM(retry_count), 0) FROM payments").fetchone()
    result["retries"] = int(retry_row[0] or 0)
    return result

def save_incident(connection: sqlite3.Connection, incident) -> None:
    connection.execute(
        """
        INSERT INTO incidents (incident_type, severity, cause, status, started_at, resolved_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            incident.incident_type.value,
            incident.severity.value,
            incident.cause,
            incident.status.value,
            incident.started_at.isoformat(),
            incident.resolved_at.isoformat() if incident.resolved_at else None,
        ),
    )
    connection.commit()


def load_recent_payments(connection: sqlite3.Connection, limit: int = 20) -> list[tuple]:
    cursor = connection.execute(
        "SELECT payment_id, amount_rubles, status, channel, terminal_id, latency_ms, created_at "
        "FROM payments ORDER BY created_at DESC LIMIT ?",
        (limit,),
    )
    return cursor.fetchall()


def load_all_payments(connection: sqlite3.Connection) -> list[tuple]:
    cursor = connection.execute(
        "SELECT payment_id, amount_rubles, status, channel, terminal_id, latency_ms, created_at "
        "FROM payments ORDER BY created_at ASC"
    )
    return cursor.fetchall()
