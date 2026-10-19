from models import PaymentStatus

# Допустимые переходы совпадают с таблицей в payment_states.md.
ALLOWED_TRANSITIONS: dict[PaymentStatus, list[PaymentStatus]] = {
    PaymentStatus.CREATED: [PaymentStatus.VALIDATING],
    PaymentStatus.VALIDATING: [PaymentStatus.RISK_CHECK, PaymentStatus.DECLINED],
    PaymentStatus.RISK_CHECK: [PaymentStatus.ROUTING, PaymentStatus.DECLINED],
    PaymentStatus.ROUTING: [PaymentStatus.PROCESSING, PaymentStatus.DECLINED, PaymentStatus.ERROR],
    PaymentStatus.PROCESSING: [PaymentStatus.APPROVED, PaymentStatus.DECLINED, PaymentStatus.ERROR],
    PaymentStatus.APPROVED: [],
    PaymentStatus.DECLINED: [],
    PaymentStatus.ERROR: [],
}

# Вероятности выбора следующего статуса (вместо равновероятного random.choice,
# при котором половина платежей отклонялась уже на первых шагах).
TRANSITION_WEIGHTS: dict[PaymentStatus, dict[PaymentStatus, float]] = {
    PaymentStatus.VALIDATING: {PaymentStatus.RISK_CHECK: 0.96, PaymentStatus.DECLINED: 0.04},
    PaymentStatus.RISK_CHECK: {PaymentStatus.ROUTING: 0.92, PaymentStatus.DECLINED: 0.08},
    PaymentStatus.ROUTING: {PaymentStatus.PROCESSING: 0.97, PaymentStatus.DECLINED: 0.01, PaymentStatus.ERROR: 0.02},
    PaymentStatus.PROCESSING: {PaymentStatus.APPROVED: 0.88, PaymentStatus.DECLINED: 0.09, PaymentStatus.ERROR: 0.03},
}


def can_transition(current: PaymentStatus, target: PaymentStatus) -> bool:
    return target in ALLOWED_TRANSITIONS[current]
