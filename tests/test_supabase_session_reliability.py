"""Offline dashboard renewal, rotation durability and owner-only idle expiry."""

import fcntl
import json
import sqlite3
import unittest
from contextlib import closing
from unittest.mock import Mock, patch

import requests
import test_supabase_backend as fixtures
from flask import session

import supabase_backend as backend


class OwnerSessionReliabilityTests(unittest.TestCase):
    setUp = fixtures.DashboardTests.setUp
    login = fixtures.DashboardTests.login

    def prepare(self):
        @self.app.get("/owner-check")
        def owner_check():
            if not self.integration.is_authenticated():
                return self.integration.auth_failure() or {
                    "reason": "login_required"
                }, (503 if self.integration.temporarily_unavailable() else 401)
            return {"outcome": "owner_verified"}

        self.login()
        self.now = 4580
        self.http = Mock()
        self.integration.client = backend.Client(
            backend.Config.from_environment(fixtures.SETTINGS), self.http
        )

    def restore_row(self, row):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute(
                "INSERT OR REPLACE INTO supabase_dashboard_sessions VALUES (?,?,?,?)",
                row,
            )

    def row(self):
        with closing(sqlite3.connect(self.path)) as conn:
            row = conn.execute(
                "SELECT token_hash,owner_id,encrypted_tokens,expires_at FROM supabase_dashboard_sessions"
            ).fetchone()
        return row

    def saved_tokens(self):
        return json.loads(self.integration.cipher.decrypt(self.row()[2]))

    def reference(self):
        with self.web.session_transaction() as state:
            return state["supabase_session"]

    def rotation(self, owner=fixtures.OWNER):
        return fixtures.response(
            {
                "access_token": "rotated-private-access",
                "refresh_token": "rotated-private-refresh",
                "expires_in": 3600,
                "user": {"id": owner},
            }
        )

    def test_accepted_rotation_is_durable_before_failed_owner_get_and_next_get_recovers(
        self,
    ):
        self.prepare()
        reference, before = self.reference(), self.row()
        calls = []

        def upstream(method, url, **kwargs):
            calls.append((method, url))
            if len(calls) == 1:
                return self.rotation()
            self.assertEqual(
                self.saved_tokens()["refresh_token"], "rotated-private-refresh"
            )
            self.assertTrue(self.saved_tokens()["verification_pending"])
            return fixtures.response(
                {"code": "unexpected_failure", "message": "provider-private-marker"},
                status=503,
            )

        self.http.request.side_effect = upstream
        with self.assertLogs(self.app.logger, level="WARNING") as logs:
            result = self.web.get("/owner-check")
        self.assertEqual(result.status_code, 503)
        self.assertEqual(
            result.json,
            {
                "stage": "owner_verification",
                "reason": "service_unavailable",
                "http_status": 503,
                "retryable": True,
            },
        )
        self.assertEqual(self.reference(), reference)
        self.assertEqual(self.row()[0:2], before[0:2])
        self.assertEqual(self.row()[3], before[3])
        self.assertTrue(self.saved_tokens()["verification_pending"])
        self.assertEqual([method for method, _ in calls], ["POST", "GET"])
        for secret in (
            "rotated-private-access",
            "rotated-private-refresh",
            "provider-private-marker",
        ):
            self.assertNotIn(secret, result.get_data(as_text=True))
            self.assertNotIn(secret, " ".join(logs.output))
            self.assertNotIn(secret.encode(), self.path.read_bytes())
        self.http.request.side_effect = None
        self.http.request.return_value = fixtures.response({"id": fixtures.OWNER})
        recovered = self.web.get("/owner-check")
        self.assertEqual(recovered.status_code, 200)
        self.assertEqual(self.http.request.call_count, 3)
        self.assertEqual(self.http.request.call_args.args[0], "GET")
        self.assertNotIn("verification_pending", self.saved_tokens())
        self.assertEqual(self.row()[3], self.now + 7 * 86400)
        self.assertEqual(self.reference(), reference)

    def test_refresh_busy_preserves_reference_denies_access_and_does_not_send_refresh(
        self,
    ):
        self.prepare()
        before, reference = self.row(), self.reference()
        with (self.path.parent / "supabase-refresh.lock").open("w") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.web.get("/owner-check")
        self.assertEqual(result.status_code, 503)
        self.assertEqual(result.json["stage"], "refresh_lock")
        self.assertEqual(result.json["reason"], "refresh_busy")
        self.http.request.assert_not_called()
        self.assertEqual(self.row(), before)
        self.assertEqual(self.reference(), reference)
        self.http.request.side_effect = [
            self.rotation(),
            fixtures.response({"id": fixtures.OWNER}),
        ]
        self.assertEqual(self.web.get("/owner-check").status_code, 200)
        self.assertEqual(
            [call.args[0] for call in self.http.request.call_args_list], ["POST", "GET"]
        )

    def test_transient_exchange_failure_never_retries_post_or_accepts_expired_access(
        self,
    ):
        self.prepare()
        self.now = 4601
        original = self.row()
        for failure, reason in (
            (requests.Timeout("private-provider-url"), "timeout"),
            (requests.ConnectionError("private-provider-url"), "network"),
            (
                fixtures.response({"message": "private-provider-body"}, 429),
                "rate_limited",
            ),
            (
                fixtures.response({"message": "private-provider-body"}, 503),
                "service_unavailable",
            ),
            (fixtures.response({"code": "conflict"}, 409), "conflict"),
        ):
            with self.subTest(reason=reason):
                self.restore_row(original)
                self.http.reset_mock()
                self.http.request.side_effect = (
                    failure if isinstance(failure, Exception) else None
                )
                if not isinstance(failure, Exception):
                    self.http.request.return_value = failure
                result = self.web.get("/owner-check")
                self.assertEqual(result.status_code, 503)
                self.assertEqual(result.json["reason"], reason)
                self.assertEqual(result.json["stage"], "refresh_exchange")
                self.http.request.assert_called_once()
                self.assertEqual(self.http.request.call_args.args[0], "POST")
                self.assertEqual(self.row()[0:2], original[0:2])
                self.assertEqual(self.row()[3], original[3])
                self.assertEqual(self.saved_tokens()["refresh_exchange_attempts"], 1)
                self.assertNotIn("private-provider", result.get_data(as_text=True))

    def test_two_uncertain_exchanges_are_reserved_and_third_never_posts(self):
        self.prepare()
        reference, initial = self.reference(), self.row()

        def uncertain(*args, **kwargs):
            self.assertEqual(
                self.saved_tokens()["refresh_exchange_attempts"],
                self.http.request.call_count,
            )
            raise requests.ReadTimeout("private-provider")

        self.http.request.side_effect = uncertain
        for attempt in (1, 2):
            self.assertEqual(self.web.get("/owner-check").status_code, 503)
            self.assertEqual(self.saved_tokens()["refresh_exchange_attempts"], attempt)
        result = self.web.get("/owner-check")
        self.assertEqual(result.status_code, 503)
        self.assertEqual(result.json["reason"], "refresh_uncertain")
        self.assertTrue(result.json["requires_sign_in"])
        self.assertEqual(self.http.request.call_count, 2)
        self.assertEqual(self.reference(), reference)
        self.assertEqual(self.row()[3], initial[3])

    def test_lost_response_has_one_marked_parent_recovery_before_owner_verification(
        self,
    ):
        self.prepare()
        self.http.request.side_effect = requests.ReadTimeout("private-provider")
        self.assertEqual(self.web.get("/owner-check").status_code, 503)
        self.http.reset_mock()
        calls = []

        def recovered(method, url, **kwargs):
            calls.append(method)
            if method == "POST":
                self.assertEqual(self.saved_tokens()["refresh_exchange_attempts"], 2)
                self.assertEqual(
                    kwargs["json"]["refresh_token"], "private-refresh-token"
                )
                return self.rotation()
            self.assertTrue(self.saved_tokens()["verification_pending"])
            return fixtures.response({"id": fixtures.OWNER})

        self.http.request.side_effect = recovered
        self.assertEqual(self.web.get("/owner-check").status_code, 200)
        self.assertEqual(calls, ["POST", "GET"])
        self.assertNotIn("refresh_exchange_attempts", self.saved_tokens())
        self.assertNotIn("verification_pending", self.saved_tokens())

    def test_proven_pre_exchange_rate_limit_keeps_prior_uncertainty_count(self):
        self.prepare()
        for uncertain in (False, True):
            with self.subTest(parent_recovery=uncertain):
                prior = self.saved_tokens()
                if uncertain:
                    prior["refresh_exchange_attempts"] = 1
                    row = list(self.row())
                    row[2] = self.integration.cipher.encrypt(json.dumps(prior).encode())
                    self.restore_row(row)
                self.http.reset_mock()
                self.http.request.side_effect = None
                self.http.request.return_value = fixtures.response(
                    {"code": "over_request_rate_limit", "message": "private-provider"},
                    429,
                )
                for _ in range(3):
                    self.assertEqual(self.web.get("/owner-check").status_code, 503)
                    self.assertEqual(self.saved_tokens(), prior)
                self.assertEqual(self.http.request.call_count, 3)
        self.http.request.side_effect = [
            self.rotation(),
            fixtures.response({"id": fixtures.OWNER}),
        ]
        self.assertEqual(self.web.get("/owner-check").status_code, 200)
        self.assertNotIn("refresh_exchange_attempts", self.saved_tokens())

    def test_reservation_storage_failure_sends_no_refresh_post(self):
        self.prepare()
        original = self.row()
        real_connection = self.integration.connection
        count = 0

        def connection():
            nonlocal count
            count += 1
            if count == 3:
                raise sqlite3.OperationalError("private-database-marker")
            return real_connection()

        with patch.object(self.integration, "connection", side_effect=connection):
            result = self.web.get("/owner-check")
        self.assertEqual(result.status_code, 503)
        self.assertEqual(result.json["reason"], "persistence")
        self.http.request.assert_not_called()
        self.assertEqual(self.row(), original)
        self.assertNotIn("private-database-marker", result.get_data(as_text=True))

    def test_accepted_candidate_save_failure_leaves_reservation_for_one_parent_recovery(
        self,
    ):
        self.prepare()
        real_connection = self.integration.connection
        count = 0

        def connection():
            nonlocal count
            count += 1
            if count == 4:
                raise sqlite3.OperationalError("private-candidate-save-marker")
            return real_connection()

        self.http.request.return_value = self.rotation()
        with patch.object(self.integration, "connection", side_effect=connection):
            result = self.web.get("/owner-check")
        self.assertEqual(result.status_code, 503)
        self.assertEqual(result.json["reason"], "persistence")
        self.http.request.assert_called_once()
        self.assertEqual(self.saved_tokens()["refresh_exchange_attempts"], 1)
        self.assertEqual(self.saved_tokens()["refresh_token"], "private-refresh-token")
        self.http.reset_mock()
        calls = []

        def recovered(method, url, **kwargs):
            calls.append(method)
            if method == "POST":
                self.assertEqual(self.saved_tokens()["refresh_exchange_attempts"], 2)
                return self.rotation()
            return fixtures.response({"id": fixtures.OWNER})

        self.http.request.side_effect = recovered
        self.assertEqual(self.web.get("/owner-check").status_code, 200)
        self.assertEqual(calls, ["POST", "GET"])
        self.assertEqual(
            self.saved_tokens()["refresh_token"], "rotated-private-refresh"
        )
        self.assertNotIn("refresh_exchange_attempts", self.saved_tokens())

    def test_initial_session_read_database_error_is_safe_temporary_failure(self):
        self.prepare()
        original = self.row()
        with patch.object(
            self.integration,
            "connection",
            side_effect=sqlite3.OperationalError("private-database-marker"),
        ), self.assertLogs(self.app.logger, level="WARNING") as logs:
            result = self.web.get("/owner-check")
        self.assertEqual(result.status_code, 503)
        self.assertEqual(result.json["reason"], "persistence")
        self.assertEqual(result.json["stage"], "session_decryption")
        self.http.request.assert_not_called()
        self.assertEqual(self.row(), original)
        self.assertNotIn("private-database-marker", result.get_data(as_text=True))
        self.assertNotIn("private-database-marker", " ".join(logs.output))

    def test_oversized_error_response_preserves_transient_http_classification(self):
        self.prepare()
        original = self.row()
        for status, reason in ((429, "rate_limited"), (500, "service_unavailable")):
            with self.subTest(status=status):
                self.restore_row(original)
                upstream = fixtures.response({}, status)
                upstream.iter_content.return_value = [
                    b"x" * (backend.AUTH_BODY_LIMIT + 1)
                ]
                self.http.request.return_value = upstream
                result = self.web.get("/owner-check")
                self.assertEqual(result.status_code, 503)
                self.assertEqual(result.json["reason"], reason)
                self.assertEqual(result.json["http_status"], status)
                upstream.close.assert_called_once()

    def test_expired_pending_pair_renews_new_token_never_original_parent(self):
        self.prepare()
        self.http.request.side_effect = [self.rotation(), fixtures.response({}, 503)]
        self.assertEqual(self.web.get("/owner-check").status_code, 503)
        self.now += 3601
        self.http.reset_mock()
        self.http.request.side_effect = [
            self.rotation(),
            fixtures.response({"id": fixtures.OWNER}),
        ]
        self.assertEqual(self.web.get("/owner-check").status_code, 200)
        self.assertEqual(
            self.http.request.call_args_list[0].kwargs["json"]["refresh_token"],
            "rotated-private-refresh",
        )
        self.assertNotIn("verification_pending", self.saved_tokens())

    def test_terminal_refresh_codes_never_retry_or_extend_idle_expiry(self):
        self.prepare()
        original = self.row()
        for code, status in (
            ("refresh_token_already_used", 400),
            ("refresh_token_not_found", 400),
            ("session_not_found", 401),
            ("session_expired", 400),
            ("unknown-private-marker", 403),
        ):
            with self.subTest(code=code):
                self.restore_row(original)
                self.http.reset_mock()
                self.http.request.return_value = fixtures.response(
                    {"code": code, "message": "private-marker"}, status
                )
                result = self.web.get("/owner-check")
                self.assertEqual(result.status_code, 401)
                self.assertFalse(result.json["retryable"])
                self.assertIsNone(self.row())
                self.http.request.assert_called_once()
                self.assertEqual(self.web.get("/owner-check").status_code, 401)
                self.http.request.assert_called_once()
                self.assertNotIn("private-marker", result.get_data(as_text=True))

    def test_other_owner_or_missing_bound_user_cannot_persist_rotation(self):
        self.prepare()
        original = self.row()
        for user in ({"id": fixtures.OTHER_OWNER}, None):
            with self.subTest(bound_user=user):
                self.restore_row(original)
                value = {
                    "access_token": "wrong-private-access",
                    "refresh_token": "wrong-private-refresh",
                    "expires_in": 3600,
                    "user": user,
                }
                self.http.reset_mock()
                self.http.request.return_value = fixtures.response(value)
                result = self.web.get("/owner-check")
                self.assertEqual(result.status_code, 401)
                self.assertEqual(result.json["reason"], "owner_mismatch")
                self.assertIsNone(self.row())
                self.http.request.assert_called_once()

    def test_owner_mismatch_after_rotation_get_invalidates_consumed_session_without_replay(
        self,
    ):
        self.prepare()
        self.http.request.side_effect = [
            self.rotation(),
            fixtures.response({"id": fixtures.OTHER_OWNER}),
        ]
        result = self.web.get("/owner-check")
        self.assertEqual(result.status_code, 401)
        self.assertEqual(result.json["reason"], "owner_mismatch")
        self.assertIsNone(self.row())
        self.assertEqual(self.http.request.call_count, 2)
        self.assertEqual(self.web.get("/owner-check").status_code, 401)
        self.assertEqual(self.http.request.call_count, 2)

    def test_malformed_accepted_token_pair_invalidates_exact_reservation(self):
        self.prepare()
        self.http.request.return_value = fixtures.response(
            {
                "access_token": "private-access",
                "refresh_token": None,
                "expires_in": 3600,
                "user": {"id": fixtures.OWNER},
            }
        )
        self.assertEqual(self.web.get("/owner-check").status_code, 401)
        self.assertIsNone(self.row())
        self.http.request.assert_called_once()
        self.assertEqual(self.web.get("/owner-check").status_code, 401)
        self.http.request.assert_called_once()

    def test_pending_owner_get_rejection_deletes_current_rotation_without_old_replay(
        self,
    ):
        self.prepare()
        self.http.request.side_effect = [self.rotation(), fixtures.response({}, 503)]
        self.assertEqual(self.web.get("/owner-check").status_code, 503)
        self.assertTrue(self.saved_tokens()["verification_pending"])
        self.http.reset_mock()
        self.http.request.side_effect = None
        self.http.request.return_value = fixtures.response(
            {"code": "session_not_found"}, 401
        )
        self.assertEqual(self.web.get("/owner-check").status_code, 401)
        self.assertIsNone(self.row())
        self.http.request.assert_called_once()
        self.assertEqual(self.http.request.call_args.args[0], "GET")
        self.assertEqual(self.web.get("/owner-check").status_code, 401)
        self.http.request.assert_called_once()

    def test_logout_or_concurrent_replacement_during_exchange_cannot_resurrect_or_overwrite(
        self,
    ):
        self.prepare()
        for delete in (False, True):
            with self.subTest(delete=delete):
                if self.row() is None:
                    break
                replacement = self.integration.cipher.encrypt(
                    json.dumps(
                        {
                            "access_token": "concurrent-access",
                            "expires_at": self.now + 500,
                        }
                    ).encode()
                )

                def upstream(*args, delete=delete, replacement=replacement, **kwargs):
                    with closing(sqlite3.connect(self.path)) as conn, conn:
                        if delete:
                            conn.execute("DELETE FROM supabase_dashboard_sessions")
                        else:
                            conn.execute(
                                "UPDATE supabase_dashboard_sessions SET encrypted_tokens=?",
                                (replacement,),
                            )
                    return self.rotation()

                self.http.reset_mock()
                self.http.request.side_effect = upstream
                self.assertEqual(self.web.get("/owner-check").status_code, 401)
                self.http.request.assert_called_once()
                if delete:
                    self.assertIsNone(self.row())
                else:
                    self.assertEqual(self.row()[2], replacement)
                    # Restore a due fictional token pair for the logout case.
                    with closing(sqlite3.connect(self.path)) as conn, conn:
                        conn.execute(
                            "UPDATE supabase_dashboard_sessions SET encrypted_tokens=?",
                            (
                                self.integration.cipher.encrypt(
                                    json.dumps(
                                        {
                                            "access_token": "private-access-token",
                                            "refresh_token": "private-refresh-token",
                                            "expires_at": 4600,
                                        }
                                    ).encode()
                                ),
                            ),
                        )

    def test_idle_expiry_rolls_only_after_live_owner_verification_never_after_denial(
        self,
    ):
        self.prepare()
        self.now = 2000
        before = self.row()[3]
        self.http.request.return_value = fixtures.response({"id": fixtures.OWNER})
        self.assertEqual(self.web.get("/owner-check").status_code, 200)
        self.assertEqual(self.row()[3], self.now + 7 * 86400)
        self.assertGreater(self.row()[3], before)
        self.now += 100
        verified_expiry = self.row()[3]
        self.http.request.return_value = fixtures.response({"id": fixtures.OTHER_OWNER})
        self.assertEqual(self.web.get("/owner-check").status_code, 401)
        self.assertEqual(self.row()[3], verified_expiry)

    def test_expired_local_reference_cannot_revive_even_with_provider_available(self):
        self.prepare()
        self.now = self.row()[3] + 1
        self.http.request.return_value = fixtures.response({"id": fixtures.OWNER})
        before = self.row()
        self.assertEqual(self.web.get("/owner-check").status_code, 401)
        self.http.request.assert_not_called()
        self.assertEqual(self.row(), before)

    def test_expiry_boundary_after_verified_get_cannot_extend_or_grant_access(self):
        self.prepare()
        self.now = 2000
        before = self.row()

        def upstream(*args, **kwargs):
            self.now = before[3] + 1
            return fixtures.response({"id": fixtures.OWNER})

        self.http.request.side_effect = upstream
        self.assertEqual(self.web.get("/owner-check").status_code, 503)
        self.assertEqual(self.row(), before)

    def test_explicit_logout_removes_pending_rotation_and_reference(self):
        self.prepare()
        self.http.request.side_effect = [
            self.rotation(),
            fixtures.response({}, status=503),
        ]
        self.assertEqual(self.web.get("/owner-check").status_code, 503)
        with self.app.test_request_context():
            session["supabase_session"] = self.reference()
            self.integration.logout()
            self.assertFalse(self.integration.is_authenticated())
        self.assertIsNone(self.row())


if __name__ == "__main__":
    unittest.main()
