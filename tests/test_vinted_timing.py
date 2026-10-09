"""Timing cannot disclose buyer data, change results, or retry operations."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import vinted_timing as timing


class BuyerTimingTests(unittest.TestCase):
    def test_route_labels_never_include_identifiers_or_unknown_path_values(self):
        routes = (
            ("GET", "/api/v2/users/current", None, "identity"),
            ("POST", "/web/api/auth/refresh", None, "renewal"),
            ("POST", "/api/v2/conversations", {"item_id": "private"}, "conversation"),
            (
                "POST",
                "/api/v2/purchases/checkout/build",
                {"purchase_items": "private"},
                "checkout_build",
            ),
            (
                "PUT",
                "/api/v2/purchases/private-checkout/checkout",
                {"components": {"payment_method": {}}},
                "checkout_load",
            ),
            (
                "PUT",
                "/api/v2/purchases/private-checkout/checkout",
                {"components": {"payment_method": {"private": "secret"}}},
                "checkout_choices",
            ),
            (
                "POST",
                "/api/v2/purchases/private-checkout/checkout/payment",
                {"checksum": "secret"},
                "payment_submit",
            ),
            (
                "GET",
                "/api/v2/purchases/private-checkout/checkout/payment",
                None,
                "payment_status",
            ),
            (
                "GET",
                "/shipping-estimation/external/shipping_orders/123/nearby_pickup_points",
                None,
                "pickup_lookup",
            ),
            ("GET", "/api/v2/private?token=secret", None, "buyer_request"),
        )
        for method, path, body, expected in routes:
            with self.subTest(expected=expected):
                self.assertEqual(timing.request_operation(method, path, body), expected)

    def test_logs_only_numeric_allowlisted_metadata_not_response_or_body(self):
        response = SimpleNamespace(
            transport_timings={
                "elapsed_ms": 1234.5,
                "connect_ms": 0,
                "tls_ms": "private-secret",
                "ttfb_ms": float("nan"),
                "new_connections": True,
                "headers": {"Authorization": "private-secret"},
                "url": "https://private-secret/",
            },
            status_code=200,
            text="private-secret",
        )
        with self.assertLogs("vinted_timing", level="INFO") as logs:
            timing.log_response_timing("payment_submit", response)
        rendered = " ".join(logs.output)
        self.assertIn("operation=payment_submit http=200", rendered)
        self.assertIn("elapsed_ms=1234.500 connect_ms=0.000", rendered)
        self.assertNotIn("private", rendered)
        self.assertNotIn("ttfb_ms", rendered)
        self.assertNotIn("new_connections", rendered)

    def test_missing_metadata_or_unrecognised_operation_produces_no_log(self):
        with patch.object(timing.logger, "info") as log:
            timing.log_response_timing("identity", SimpleNamespace())
            timing.log_response_timing(
                "private-secret", SimpleNamespace(transport_timings={"elapsed_ms": 1})
            )
            timing.log_response_timing(
                "identity",
                SimpleNamespace(
                    transport_timings={"elapsed_ms": float("inf"), "connect_ms": -1}
                ),
            )
        log.assert_not_called()

    def test_one_call_keeps_same_result_even_if_logging_fails(self):
        result = object()
        operation = Mock(return_value=result)
        measured = timing.timed_operation("payment_result_save")(operation)
        with patch.object(
            timing.logger, "info", side_effect=RuntimeError("log unavailable")
        ):
            self.assertIs(measured("private-input"), result)
        operation.assert_called_once_with("private-input")

    def test_original_exception_is_preserved_without_retry(self):
        error = RuntimeError("private-operation-error")
        operation = Mock(side_effect=error)
        measured = timing.timed_operation("purchase")(operation)
        with self.assertLogs("vinted_timing", level="INFO") as logs, self.assertRaises(
            RuntimeError
        ) as raised:
            measured()
        self.assertIs(raised.exception, error)
        operation.assert_called_once()
        self.assertIn("outcome=raised", " ".join(logs.output))
        self.assertNotIn("private", " ".join(logs.output))

    def test_known_duration_and_unknown_operation_are_explicit(self):
        measured = timing.timed_operation("session_save")(lambda: None)
        with patch(
            "vinted_timing.time.perf_counter", side_effect=[10.0, 10.025]
        ), self.assertLogs("vinted_timing", level="INFO") as logs:
            measured()
        self.assertIn("elapsed_ms=25.000", " ".join(logs.output))
        with self.assertRaises(ValueError):
            timing.timed_operation("private-secret")


if __name__ == "__main__":
    unittest.main()
