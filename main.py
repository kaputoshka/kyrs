import csv
import sys
from datetime import datetime
from PySide6.QtCore import QTimer,Qt
from pathlib import Path
from PySide6.QtGui import QBrush, QColor, QPainter

from PySide6.QtCharts import (
    QChart,
    QChartView,
    QLineSeries,
    QValueAxis
)

from PySide6.QtWidgets import (
    QCheckBox,
    QHeaderView,
    QFileDialog,
    QScrollArea,
    QFrame,
    QMessageBox,
    QApplication,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QTableWidget,
    QVBoxLayout,
    QWidget,
    QTableWidgetItem,
    QComboBox,
    QSlider,
    QLineEdit,
    QListWidget,
)

from models import Payment, PaymentStatus
from engine import TERMINALS, SimulationEngine
from incidents import IncidentStatus
from storage import load_all_payments, load_recent_payments



def resource_path(filename: str) -> Path:
    """Путь к файлу и при обычном запуске, и внутри собранного .exe."""
    if getattr(sys, "_MEIPASS", None):
        return Path(sys._MEIPASS) / filename
    return Path(__file__).with_name(filename)


class MainWindow(QMainWindow):
    def __init__(self) -> None:

        super().__init__()
        self.setWindowTitle("Вахапов Артём ЭАС-401С")
        self.setMinimumSize(900, 600)
        self.engine = SimulationEngine()

        title_label = QLabel("Вахапов Артём  <span style='color:#5277F7'>RT</span>  <span style='font-size:15px;font-weight:500;color:#667085'>· Платёжный мониторинг</span>")
        title_label.setTextFormat(Qt.RichText)
        title_label.setObjectName("titleLabel")
        central_widget = QWidget()
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(26, 22, 26, 26)
        main_layout.setSpacing(18)

        header_layout = QHBoxLayout()
        header_layout.addWidget(title_label)
        header_layout.addStretch(1)

        self.status_label = QLabel("Симуляция остановлена")
        self.status_label.setObjectName("statusLabel")
        header_layout.addWidget(self.status_label)
        controls_layout = QHBoxLayout()
        operation_label = QLabel("Операционный обзор")
        operation_label.setObjectName("sectionTitle")
        controls_layout.addWidget(operation_label)

        start_button = QPushButton("Старт")
        pause_button = QPushButton("Пауза")
        stop_button = QPushButton("Стоп")
        start_button.setObjectName("startButton")
        pause_button.setObjectName("pauseButton")
        stop_button.setObjectName("stopButton")

        controls_layout.addStretch(stretch=100)

        start_button.clicked.connect(self.handle_start)
        pause_button.clicked.connect(self.handle_pause)
        stop_button.clicked.connect(self.handle_stop)

        controls_layout.addWidget(start_button)

        controls_layout.addWidget(pause_button)
        controls_layout.addWidget(stop_button)

        main_layout.addLayout(header_layout)
        main_layout.addLayout(controls_layout)

        central_widget.setObjectName("centralRoot")
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        scroll_area.setWidget(central_widget)
        self.setCentralWidget(scroll_area)
        self.clock_label = QLabel()
        self.clock_label.setObjectName("clockLabel")
        header_layout.addWidget(self.clock_label)



        self.clock_timer = QTimer(self)
        self.clock_timer.timeout.connect(self.update_clock)
        self.clock_timer.start(1000)
        self.update_clock()

        self.simulation_timer = QTimer(self)
        self.simulation_timer.timeout.connect(self.run_simulation_tick)
        self.simulation_timer.start(800)

        kpi_layout = QHBoxLayout()

        operations_card, self.operations_value_label, self.operations_delta_label = \
            self._build_kpi_card("kpiCard", "Операций / с")
        latency_card, self.average_latency_value_label, self.latency_delta_label = \
            self._build_kpi_card("kpiCardLatency", "Средняя задержка, мс")
        queue_card, self.queue_length_value_label, self.queue_delta_label = \
            self._build_kpi_card("kpiCardQueue", "Очередь сообщений")
        approval_card, self.approval_rate_value_label, self.approval_delta_label = \
            self._build_kpi_card("kpiCardApproval", "Доля одобрений")

        for card in (operations_card, latency_card, queue_card, approval_card):
            kpi_layout.addWidget(card)

        kpi_layout.setSpacing(12)
        main_layout.addLayout(kpi_layout)

        content_layout = QHBoxLayout()
        content_layout.setSpacing(16)

        left_column = QVBoxLayout()
        left_column.setSpacing(12)

        payments_title_label = QLabel("Последние операции")
        left_column.addWidget(payments_title_label)

        self.payments_table = QTableWidget(0, 7)
        self.payments_table.setAlternatingRowColors(True)
        self.payments_table.setShowGrid(False)
        self.payments_table.setMinimumHeight(310)
        self.payments_table.verticalHeader().setDefaultSectionSize(38)
        self.payments_table.setHorizontalHeaderLabels(
            ["ID",
             "Сумма, ₽",
             "Статус",
             "Канал",
             "Терминал",
             "Задержка, мс",
             "Время"]
        )
        self.payments_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.payments_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.payments_table.verticalHeader().setVisible(False)
        self.payments_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        left_column.addWidget(self.payments_table)
        self._load_history()

        # сюда же, если занятие №8 уже сделано, переносим charts_layout:
        # left_column.addLayout(charts_layout)

        right_column = QVBoxLayout()
        right_column.setSpacing(12)

        self.right_column = right_column

        content_layout.addLayout(left_column, stretch=2)
        content_layout.addLayout(right_column, stretch=1)

        main_layout.addLayout(content_layout)



        header_layout.setSpacing(14)
        controls_layout.setSpacing(10)
        kpi_layout.setSpacing(12)

        charts_layout = QHBoxLayout()

        self.traffic_chart_view, self.traffic_series = self._build_chart(
            "Входящий поток", "оп/с"
        )
        self.latency_chart_view, self.latency_series = self._build_chart(
            "Задержка шлюза", "мс"
        )
        self.queue_chart_view, self.queue_series = self._build_chart(
            "Длина очереди", "платежей"
        )

        charts_layout.addWidget(self.traffic_chart_view)
        charts_layout.addWidget(self.latency_chart_view)
        charts_layout.addWidget(self.queue_chart_view)

        main_layout.addLayout(charts_layout)
        controls_panel = QGroupBox("Панель управления")
        controls_panel_layout = QVBoxLayout(controls_panel)

        gateway_row = QHBoxLayout()
        gateway_row.addWidget(QLabel("Активный шлюз"))
        self.gateway_selector = QComboBox()
        self.gateway_selector.addItems(["Основной", "Резервный"])
        gateway_row.addWidget(self.gateway_selector)
        controls_panel_layout.addLayout(gateway_row)

        intensity_row = QHBoxLayout()
        intensity_row.addWidget(QLabel("Интенсивность"))
        self.intensity_slider = QSlider(Qt.Horizontal)
        self.intensity_slider.setRange(2, 30)
        self.intensity_slider.setValue(10)
        intensity_row.addWidget(self.intensity_slider)
        controls_panel_layout.addLayout(intensity_row)

        retry_button = QPushButton("Повторить обработку")
        controls_panel_layout.addWidget(retry_button)

        terminal_row = QHBoxLayout()
        terminal_row.addWidget(QLabel("Терминал"))
        self.terminal_selector = QComboBox()
        self.terminal_selector.addItems(TERMINALS)
        terminal_row.addWidget(self.terminal_selector)
        controls_panel_layout.addLayout(terminal_row)

        terminal_buttons_row = QHBoxLayout()
        block_button = QPushButton("Заблокировать")
        unblock_button = QPushButton("Разблокировать")
        terminal_buttons_row.addWidget(block_button)
        terminal_buttons_row.addWidget(unblock_button)
        controls_panel_layout.addLayout(terminal_buttons_row)

        self.blocked_label = QLabel("Заблокированы: —")
        controls_panel_layout.addWidget(self.blocked_label)

        self.auto_mode_checkbox = QCheckBox("Автоматический режим")
        controls_panel_layout.addWidget(self.auto_mode_checkbox)
        self.auto_mode_checkbox.toggled.connect(self.handle_toggle_auto_mode)

        self.events_list = QListWidget()
        self.events_list.setWordWrap(True)
        self.events_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        controls_panel_layout.addWidget(QLabel("События"))
        controls_panel_layout.addWidget(self.events_list)

        force_gateway_button = QPushButton("Смоделировать отказ шлюза")
        force_gateway_button.clicked.connect(self.handle_force_gateway_incident)
        controls_panel_layout.addWidget(force_gateway_button)

        restore_gateway_button = QPushButton("Восстановить основной шлюз")
        restore_gateway_button.clicked.connect(self.handle_restore_primary_gateway)
        controls_panel_layout.addWidget(restore_gateway_button)

        force_queue_button = QPushButton("Смоделировать перегрузку очереди")
        force_queue_button.clicked.connect(self.handle_force_queue_incident)
        controls_panel_layout.addWidget(force_queue_button)

        force_latency_button = QPushButton("Смоделировать рост задержки")
        force_latency_button.clicked.connect(self.handle_force_latency_incident)
        controls_panel_layout.addWidget(force_latency_button)

        export_button = QPushButton("Экспорт истории в CSV")
        export_button.clicked.connect(self.handle_export_history)
        controls_panel_layout.addWidget(export_button)

        self.right_column.addWidget(controls_panel)

        incidents_box = QGroupBox("Активные аварии")
        incidents_box_layout = QVBoxLayout(incidents_box)
        self.incidents_list = QListWidget()
        self.incidents_list.setWordWrap(True)
        self.incidents_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        incidents_box_layout.addWidget(self.incidents_list)
        self.right_column.addWidget(incidents_box)
        self._incident_states: dict[int, IncidentStatus] = {}
        self._kpi_snapshot: dict[str, int] | None = None
        self._kpi_ticks = 0

        self.gateway_selector.currentTextChanged.connect(self.handle_switch_gateway)
        self.intensity_slider.valueChanged.connect(self.handle_set_intensity)
        retry_button.clicked.connect(self.handle_retry_processing)
        block_button.clicked.connect(self.handle_block_terminal)
        unblock_button.clicked.connect(self.handle_unblock_terminal)

    def handle_toggle_auto_mode(self, enabled: bool) -> None:
        self.engine.auto_mode = enabled
        self.gateway_selector.setEnabled(not enabled)
        self.intensity_slider.setEnabled(not enabled)
        self._log_event_text(
            f"Автоматический режим {'включён' if enabled else 'выключен'}"
        )

    def _log_event_text(self, text: str) -> None:
        self.events_list.insertItem(0, text)
        if self.events_list.count() > 50:
            self.events_list.takeItem(self.events_list.count() - 1)

    def _sync_controls_with_engine(self) -> None:
        self.gateway_selector.blockSignals(True)
        self.gateway_selector.setCurrentText(self.engine.active_gateway)
        self.gateway_selector.blockSignals(False)
        self.intensity_slider.blockSignals(True)
        self.intensity_slider.setValue(round(self.engine.intensity * 10))
        self.intensity_slider.blockSignals(False)

    def update_clock(self) -> None:
        current_time = datetime.now().strftime("%H:%M:%S")
        self.clock_label.setText(current_time)

    def handle_start(self) -> None:
        self.engine.start()
        self.status_label.setText("Симуляция активна")

    def handle_pause(self) -> None:
        self.engine.pause()
        self.status_label.setText("Симуляция приостановлена")

    def _confirm(self, text: str) -> bool:
        answer = QMessageBox.question(
            self,
            "Подтверждение",
            text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def handle_stop(self) -> None:
        if not self._confirm("Остановить симуляцию и очистить таблицу?"):
            return

        self.engine.stop()
        self.status_label.setText("Симуляция остановлена")
        self._sync_controls_with_engine()
        self._update_blocked_label()
        self.payments_table.setRowCount(0)
        self._incident_states.clear()
        self._clear_kpi_deltas()
        self.update_kpi_cards()
        self.update_charts()
        self.update_incidents_panel()

    def run_simulation_tick(self) -> None:
        events_before = len(self.engine.events)

        changed_payments = self.engine.tick()
        for payment in changed_payments:
            self.upsert_payment_row(payment)

        for event in self.engine.events[events_before:]:
            self._log_event(event)
        self._sync_controls_with_engine()

        self.update_kpi_cards()
        self.update_charts()
        self.update_incidents_panel()

    STATUS_COLORS = {
        PaymentStatus.APPROVED: "#15803D",
        PaymentStatus.DECLINED: "#B45309",
        PaymentStatus.ERROR: "#B91C1C",
    }
    MAX_TABLE_ROWS = 200

    def _set_table_row(self, row: int, values: list[str], status: PaymentStatus) -> None:
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            if column == 2 and status in self.STATUS_COLORS:
                item.setForeground(QBrush(QColor(self.STATUS_COLORS[status])))
            self.payments_table.setItem(row, column, item)

    def upsert_payment_row(self, payment: Payment) -> None:
        row = self._find_row_by_id(payment.payment_id)
        if row is None:
            row = self.payments_table.rowCount()
            self.payments_table.insertRow(row)

        self._set_table_row(
            row,
            [
                payment.payment_id,
                f"{payment.amount_rubles:,}".replace(",", " "),
                payment.status.value,
                payment.channel,
                payment.terminal_id,
                str(payment.latency_ms),
                payment.created_at.strftime("%H:%M:%S"),
            ],
            payment.status,
        )

        # таблица не должна расти бесконечно: полная история лежит в базе
        while self.payments_table.rowCount() > self.MAX_TABLE_ROWS:
            self.payments_table.removeRow(0)

    def _find_row_by_id(self, payment_id: str) -> int | None:
        for row in range(self.payments_table.rowCount()):
            if self.payments_table.item(row, 0).text() == payment_id:
                return row
        return None

    KPI_DELTA_WINDOW = 10  # динамика KPI считается за это число тактов

    def update_kpi_cards(self) -> None:
        payments = self.engine.payments
        total = len(payments)

        finished = [p for p in payments if p.status in (
            PaymentStatus.APPROVED, PaymentStatus.DECLINED, PaymentStatus.ERROR
        )]
        approved = [p for p in payments if p.status == PaymentStatus.APPROVED]
        in_queue = total - len(finished)

        traffic = self.engine.last_traffic_reading
        operations_per_second = round(traffic.value) if traffic else 0
        avg_latency = round(sum(p.latency_ms for p in payments) / total) if payments else 0
        approval_rate = round(len(approved) / len(finished) * 100) if finished else 0

        self.operations_value_label.setText(str(operations_per_second))
        self.average_latency_value_label.setText(str(avg_latency))
        self.queue_length_value_label.setText(str(in_queue))
        self.approval_rate_value_label.setText(f"{approval_rate}%")

        current = {
            "ops": operations_per_second,
            "latency": avg_latency,
            "queue": in_queue,
            "approval": approval_rate,
        }
        self._update_kpi_deltas(current)

    def _update_kpi_deltas(self, current: dict[str, int]) -> None:
        if not self.engine.is_running:
            return
        if self._kpi_snapshot is None:
            self._kpi_snapshot = current
            return

        self._kpi_ticks += 1
        if self._kpi_ticks % self.KPI_DELTA_WINDOW:
            return

        previous = self._kpi_snapshot
        # (метка, ключ, рост — это хорошо?)
        for label, key, higher_is_better in (
            (self.operations_delta_label, "ops", True),
            (self.latency_delta_label, "latency", False),
            (self.queue_delta_label, "queue", False),
            (self.approval_delta_label, "approval", True),
        ):
            self._set_delta(label, current[key] - previous[key], higher_is_better)
        self._kpi_snapshot = current

    def _clear_kpi_deltas(self) -> None:
        self._kpi_snapshot = None
        self._kpi_ticks = 0
        for label in (
            self.operations_delta_label,
            self.latency_delta_label,
            self.queue_delta_label,
            self.approval_delta_label,
        ):
            label.setText("")

    @staticmethod
    def _set_delta(label: QLabel, delta: int, higher_is_better: bool) -> None:
        if delta == 0:
            label.setText("")
            return
        arrow = "▲" if delta > 0 else "▼"
        label.setText(f"{arrow} {abs(delta)}")
        # kpiDeltaUp — зелёный стиль («стало лучше»), kpiDeltaDown — красный
        is_good = (delta > 0) == higher_is_better
        label.setObjectName("kpiDeltaUp" if is_good else "kpiDeltaDown")
        label.style().unpolish(label)
        label.style().polish(label)

    def _build_chart(self, title: str, y_title: str) -> tuple[QChartView, QLineSeries]:
        series = QLineSeries()

        chart = QChart()
        chart.addSeries(series)
        chart.setTitle(title)
        chart.legend().hide()

        axis_x = QValueAxis()
        axis_x.setTitleText("Такт, шт.")
        axis_x.setLabelFormat("%d")
        chart.addAxis(axis_x, Qt.AlignBottom)
        series.attachAxis(axis_x)

        axis_y = QValueAxis()
        axis_y.setTitleText(y_title)
        chart.addAxis(axis_y, Qt.AlignLeft)
        series.attachAxis(axis_y)

        view = QChartView(chart)
        view.setMinimumHeight(225)
        view.setStyleSheet("background: #FFFFFF; border: 1px solid #E3E8F0; border-radius: 12px;")
        chart.setBackgroundBrush(QBrush(QColor("#FFFFFF")))
        chart.setTitleBrush(QBrush(QColor("#344054")))
        chart.setTitleFont(chart.titleFont())
        view.setRenderHint(QPainter.Antialiasing)
        return view, series

    def update_charts(self) -> None:
        self._fill_series(self.traffic_series, self.engine.traffic_history)
        self._fill_series(self.latency_series, self.engine.latency_history)
        self._fill_series(self.queue_series, self.engine.queue_history)

    def _build_kpi_card(self, object_name: str, title: str) -> tuple[QGroupBox, QLabel, QLabel]:
        card = QGroupBox(title)
        card.setObjectName(object_name)
        card_layout = QVBoxLayout(card)

        header_row = QHBoxLayout()
        value_label = QLabel("0")
        value_label.setObjectName("kpiValue")
        delta_label = QLabel("")
        delta_label.setObjectName("kpiDeltaUp")
        header_row.addWidget(value_label)
        header_row.addStretch(1)
        header_row.addWidget(delta_label)

        card_layout.addLayout(header_row)
        return card, value_label, delta_label

    @staticmethod
    def _fill_series(series: QLineSeries, history) -> None:
        series.clear()
        for index, value in enumerate(history):
            series.append(index, value)

        chart = series.chart()
        if history:
            chart.axes(Qt.Horizontal)[0].setRange(0, max(len(history) - 1, 1))
            chart.axes(Qt.Vertical)[0].setRange(0, max(history) * 1.2 or 1)



    def handle_switch_gateway(self, target: str) -> None:
        event = self.engine.switch_gateway(target)
        self._log_event(event)

    def handle_set_intensity(self, raw_value: int) -> None:
        event = self.engine.set_intensity(raw_value / 10)
        self._log_event(event)

    def handle_retry_processing(self) -> None:
        before = {p.payment_id: (p.status, p.retry_count) for p in self.engine.payments}
        event = self.engine.retry_processing()
        self._log_event(event)
        for payment in self.engine.payments:
            if before.get(payment.payment_id) != (payment.status, payment.retry_count):
                self.upsert_payment_row(payment)
        self.update_kpi_cards()

    def handle_block_terminal(self) -> None:
        terminal_id = self.terminal_selector.currentText()
        if not self._confirm(f"Заблокировать терминал {terminal_id}?"):
            return

        event = self.engine.block_terminal(terminal_id)
        self._log_event(event)
        self._update_blocked_label()

    def handle_unblock_terminal(self) -> None:
        event = self.engine.unblock_terminal(self.terminal_selector.currentText())
        self._log_event(event)
        self._update_blocked_label()

    def _update_blocked_label(self) -> None:
        blocked = ", ".join(sorted(self.engine.blocked_terminals)) or "—"
        self.blocked_label.setText(f"Заблокированы: {blocked}")

    def _log_event(self, event) -> None:
        self.events_list.insertItem(0, event.as_log_line())
        if self.events_list.count() > 50:
            self.events_list.takeItem(self.events_list.count() - 1)

    # ---------- занятие №11: аварии ----------

    def handle_force_gateway_incident(self) -> None:
        if self.engine.active_gateway != "Основной":
            self._log_event_text("Отказ шлюза: сначала переключитесь на «Основной» шлюз")
            return
        self.engine.availability_sensor.fail(40.0)
        self._log_event_text("Демонстрация: доступность шлюза резко упала")

    def handle_restore_primary_gateway(self) -> None:
        event = self.engine.restore_primary_gateway()
        self._log_event(event)

    def handle_force_queue_incident(self) -> None:
        self.engine.inject_traffic_burst(30)
        self._log_event_text("Демонстрация: всплеск входящих платежей")

    def handle_force_latency_incident(self) -> None:
        self.engine.inject_latency_spike()
        self._log_event_text("Демонстрация: задержка шлюза увеличена до аварийного уровня")

    def update_incidents_panel(self) -> None:
        self.incidents_list.clear()
        for incident in self.engine.incidents:
            previous = self._incident_states.get(id(incident))
            if previous is None:
                self._log_event_text(
                    f"Авария: {incident.incident_type.value} — {incident.cause}"
                )
            elif incident.status == IncidentStatus.RESOLVED and previous != IncidentStatus.RESOLVED:
                self._log_event_text(f"Авария устранена: {incident.incident_type.value}")
            self._incident_states[id(incident)] = incident.status

            if incident.status == IncidentStatus.RESOLVED:
                continue
            line = f"[{incident.status.value}] {incident.incident_type.value} — {incident.cause}"
            self.incidents_list.addItem(line)
            self.incidents_list.item(self.incidents_list.count() - 1).setToolTip(
                "\n".join(incident.log)
            )

    # ---------- занятие №12: история из базы ----------

    def _load_history(self) -> None:
        # reversed: из базы строки приходят «новые первыми», в таблице нужен
        # хронологический порядок, чтобы новые платежи добавлялись снизу
        for row in reversed(load_recent_payments(self.engine.db)):
            payment_id, amount, status, channel, terminal, latency, created_at = row
            table_row = self.payments_table.rowCount()
            self.payments_table.insertRow(table_row)
            self._set_table_row(
                table_row,
                [
                    payment_id,
                    f"{amount:,}".replace(",", " "),
                    status,
                    channel,
                    terminal,
                    str(latency),
                    created_at[11:19],
                ],
                PaymentStatus(status),
            )

    # ---------- занятие №13: экспорт ----------

    def handle_export_history(self) -> None:
        file_path, _ = QFileDialog.getSaveFileName(
            self, "Экспорт истории", "payments.csv", "CSV (*.csv)"
        )
        if not file_path:
            return

        # utf-8-sig: с BOM Excel на Windows корректно показывает кириллицу.
        # Экспортируется вся история из базы, а не только видимые в таблице строки.
        history = load_all_payments(self.engine.db)
        with open(file_path, "w", newline="", encoding="utf-8-sig") as file:
            writer = csv.writer(file)
            writer.writerow(["ID", "Сумма, ₽", "Статус", "Канал", "Терминал", "Задержка, мс", "Время"])
            for payment_id, amount, status, channel, terminal, latency, created_at in history:
                writer.writerow([payment_id, amount, status, channel, terminal, latency,
                                 created_at[:19].replace("T", " ")])
        self._log_event_text(f"История экспортирована ({len(history)} зап.): {file_path}")


app = QApplication(sys.argv)
style_path = resource_path("styles.qss")
app.setStyleSheet(style_path.read_text(encoding="utf-8"))


window = MainWindow()
window.show()
sys.exit(app.exec())