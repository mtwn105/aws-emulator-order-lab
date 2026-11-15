"""Offline checks for the dashboard's pure helpers. No emulator calls."""
import unittest

from app import invoice_cents, order_row, validate_order


class InvoiceTests(unittest.TestCase):
    def test_total_is_quantity_times_unit_price(self):
        self.assertEqual(invoice_cents(1), 2500)
        self.assertEqual(invoice_cents(4), 10000)

    def test_total_accepts_numeric_strings(self):
        self.assertEqual(invoice_cents("3"), 7500)


class ValidationTests(unittest.TestCase):
    def test_accepts_valid_order(self):
        self.assertIsNone(validate_order(
            {"orderId": "ui-abc123", "quantity": 3, "failure": "once"}, 5))

    def test_rejects_bad_quantity_and_id(self):
        self.assertIn("Quantity", validate_order(
            {"orderId": "ui-abc123", "quantity": 0}, 5))
        self.assertIn("Quantity", validate_order(
            {"orderId": "ui-abc123", "quantity": True}, 5))
        self.assertIn("order ID", validate_order({"orderId": "bad id", "quantity": 2}, 5))

    def test_rejects_unknown_failure_mode(self):
        self.assertIn("failure", validate_order(
            {"orderId": "ui-abc123", "quantity": 2, "failure": "sometimes"}, 5))


class RowTests(unittest.TestCase):
    def test_row_uses_stored_state_and_invoice(self):
        item = {"orderId": {"S": "ui-abc123"}, "quantity": {"N": "3"},
                "failure": {"S": "once"}, "status": {"S": "EVENT_ACCEPTED"},
                "attempts": {"N": "2"}}
        execution = {"name": "ui-abc123", "status": "SUCCEEDED"}
        row = order_row(item, execution, True)
        self.assertEqual(row["id"], "ui-abc123")
        self.assertEqual(row["attempts"], 2)
        self.assertEqual(row["invoiceCents"], 7500)
        self.assertEqual(row["executionStatus"], "SUCCEEDED")

    def test_row_without_invoice_has_no_total(self):
        item = {"orderId": {"S": "ui-dead"}, "quantity": {"N": "2"},
                "failure": {"S": "always"}, "status": {"S": "FAILED"},
                "attempts": {"N": "3"}}
        row = order_row(item, {"name": "ui-dead", "status": "FAILED"}, False)
        self.assertIsNone(row["invoiceCents"])
        self.assertEqual(row["status"], "FAILED")


if __name__ == "__main__":
    unittest.main()
