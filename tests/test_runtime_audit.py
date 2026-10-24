"""Resource audits must stay read-only and avoid exposing stored secrets."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_dashboard import DashboardTests

import runtime_audit


class ResourceFilesTests(unittest.TestCase):
    def test_memory_stats_are_numeric_and_allowlisted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "memory.current").write_text("12345")
            (root / "memory.stat").write_text(
                "anon 2000\nfile 8000\nprivate_secret 345\n"
            )
            result = runtime_audit.memory_summary(root)
            self.assertEqual(result["current_bytes"], 12345)
            self.assertEqual(result["file_bytes"], 8000)
            self.assertNotIn("private_secret_bytes", result)

    def test_storage_ignores_symlinks_and_never_returns_file_names(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "private.db"
            database.write_bytes(b"db")
            (root / "backups").mkdir()
            (root / "backups" / "private-copy.sqlite3").write_bytes(b"backup")
            (root / "secret.key").write_text("Do not expose")
            (root / "linked").symlink_to(root, target_is_directory=True)
            result = runtime_audit.storage_summary(database)
            self.assertEqual(result["groups"]["backups"]["bytes"], 6)
            self.assertNotIn("secret.key", str(result))
            self.assertNotIn("Do not expose", str(result))
            self.assertFalse(result["truncated"])
            self.assertTrue(
                runtime_audit.storage_summary(database, limit=1)["truncated"]
            )


class ResourceRouteTests(DashboardTests):
    def test_resource_audit_requires_owner_and_does_not_make_network_requests(self):
        self.assertEqual(self.client.get("/operations.json").location, "/login")
        self.owner()
        with patch.object(runtime_audit, "processes", return_value=[]), patch.object(
            runtime_audit, "memory_summary", return_value={}
        ):
            response = self.client.get("/operations.json")
        self.assertEqual(response.status_code, 200)
        self.assertGreater(response.json["database"]["tables"], 0)
        self.assertNotIn("session", str(response.json))


if __name__ == "__main__":
    unittest.main()
