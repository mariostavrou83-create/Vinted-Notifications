import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pilot.app import create_app
from pilot.windows_start import local_environment


class WindowsPilotSettingsTests(unittest.TestCase):
    def test_only_local_settings_can_enable_network_services(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            "PILOT_SCAVIO_ENABLED": "1", "PILOT_SCAVIO_API_KEY": "external",
            "FINDER_PILOT_TELEGRAM_BOT_TOKEN": "external", "FINDER_PARTNER_TOKEN": "external",
        }):
            d = Path(tmp)
            self.assertFalse(any("external" == value for key, value in local_environment(d).items()
                                 if key.startswith(("FINDER_", "PILOT_"))))
            self.assertEqual(local_environment(d)["FINDER_PILOT_TELEGRAM_MODE"], "polling")
            cfg = d / "pilot-local.ini"
            cfg.write_text("[local]\nPILOT_PUBLIC_SEARCH_ENABLED=1\nPILOT_SCAVIO_API_KEY=local-test\n"
                           "PILOT_PUBLIC_SEARCH_MAX_REQUESTS=10\nFINDER_PILOT_TELEGRAM_BOT_USERNAME=privatebot\n"
                           "FINDER_PILOT_TELEGRAM_BOT_TOKEN=local-token\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "PILOT_PUBLIC_SEARCH_DAILY_LIMIT"):
                local_environment(d)
            with cfg.open("a", encoding="utf-8") as f:
                f.write("PILOT_PUBLIC_SEARCH_DAILY_LIMIT=2\n")
            env = local_environment(d)
            self.assertEqual(env["PILOT_SCAVIO_API_KEY"], "local-test")
            self.assertEqual(env["PILOT_PUBLIC_SEARCH_DAILY_LIMIT"], "2")
            self.assertEqual(env["FINDER_PILOT_TELEGRAM_BOT_TOKEN"], "local-token")
            self.assertNotIn("FINDER_PARTNER_TOKEN", env)
            self.assertNotIn("PILOT_MISTRAL_API_KEY", env)
            cfg.write_text("[local]\nnot a key token=SECRET\n")
            with self.assertRaises(ValueError) as result:
                local_environment(d)
            self.assertNotIn("SECRET", str(result.exception))

    def test_legacy_model_and_comparison_keys_stay_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "pilot-local.ini").write_text(
                "[local]\nPILOT_MISTRAL_ENABLED=1\nPILOT_MISTRAL_API_KEY=test-old-key\n"
                "PILOT_SCAVIO_ENABLED=1\nPILOT_SCAVIO_MAX_SEARCHES=20\n"
            )
            env = local_environment(directory)
            self.assertNotIn("PILOT_MISTRAL_API_KEY", env)
            self.assertNotIn("PILOT_SCAVIO_ENABLED", env)

if __name__ == "__main__":
    unittest.main()
