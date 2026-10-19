import unittest

from models import PaymentStatus
from transitions import can_transition


class TransitionsTestCase(unittest.TestCase):
    def test_created_cannot_jump_to_approved(self) -> None:
        self.assertFalse(can_transition(PaymentStatus.CREATED, PaymentStatus.APPROVED))

    def test_created_can_go_to_validating(self) -> None:
        self.assertTrue(can_transition(PaymentStatus.CREATED, PaymentStatus.VALIDATING))

    def test_final_statuses_have_no_transitions(self) -> None:
        for status in (PaymentStatus.APPROVED, PaymentStatus.DECLINED, PaymentStatus.ERROR):
            self.assertFalse(can_transition(status, PaymentStatus.CREATED))


if __name__ == "__main__":
    unittest.main()
