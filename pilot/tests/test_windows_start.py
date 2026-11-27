import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pilot.app import create_app
from pilot.pricing import _mistral_reviewer
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

    @patch("pilot.pricing.requests.post")
    def test_model_cache_hits_and_restarts_respect_explicit_request_cap(self, upstream):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"PILOT_MISTRAL_MAX_REVIEWS": "1"}):
            config = {"TESTING":True, "DATABASE":os.path.join(tmp, "pilot.sqlite")}
            upstream.return_value.status_code = 200
            upstream.return_value.json.return_value = {"choices":[{"message":{"content":'{"matches":[0]}'}}]}
            _mistral_reviewer.cache_clear()
            app = create_app(config)
            with app.app_context():
                classify = _mistral_reviewer("fixture", "fixture-model")
                self.assertEqual(classify({"title":"demo"}, [{"title":"coat"}]), [0])
                self.assertEqual(classify({"title":"demo"}, [{"title":"coat"}]), [0])
            _mistral_reviewer.cache_clear()
            with create_app(config).app_context():
                with self.assertRaisesRegex(ValueError, "cap reached"):
                    _mistral_reviewer("fixture","fixture-model")({"title":"new listing"}, [{"title":"coat"}])
            upstream.assert_called_once()
            _mistral_reviewer.cache_clear()


if __name__ == "__main__":
    unittest.main()
