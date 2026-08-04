import os
import unittest
from unittest.mock import patch


os.environ.setdefault("CONFIG_FILE", "/tmp/turk-arr-bridge-test-config.json")

import bridge  # noqa: E402


class CaptchaPageCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.old_active = bridge._captcha_request_active
        self.old_sitekey = bridge._pending_captcha_sitekey
        self.old_host = bridge._pending_captcha_host

    def tearDown(self):
        bridge._captcha_request_active = self.old_active
        bridge._pending_captcha_sitekey = self.old_sitekey
        bridge._pending_captcha_host = self.old_host

    def test_extracts_current_sitekey_from_tracker_html(self):
        html = (
            '<script src="https://hcaptcha.com/1/api.js"></script>'
            '<div class="h-captcha" '
            'data-sitekey="new-sitekey_12345678901234567890"></div>'
        )
        self.assertEqual(
            bridge._extract_hcaptcha_sitekey(html),
            "new-sitekey_12345678901234567890",
        )
        self.assertEqual(bridge._extract_hcaptcha_sitekey("<html></html>"), "")

    def test_active_page_uses_tracker_host_and_same_origin_callbacks(self):
        bridge._captcha_request_active = True
        bridge._pending_captcha_sitekey = "dynamic-sitekey_12345678901234567890"
        bridge._pending_captcha_host = "turktorrent.us"

        with patch.object(bridge, "refresh_title_cache"):
            response = bridge.app.test_client().get(
                "/captcha?request=123", base_url="https://nas.example.ts.net"
            )

        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertIn("https://hcaptcha.com/1/api.js?", body)
        self.assertIn("host=turktorrent.us", body)
        self.assertIn("dynamic-sitekey_12345678901234567890", body)
        self.assertIn("fetch('/captcha-callback'", body)
        self.assertIn("fetch('/captcha-status?ts='", body)
        self.assertNotIn("http://nas.example.ts.net/captcha-callback", body)

    def test_status_endpoint_is_not_cacheable(self):
        with patch.object(bridge, "refresh_title_cache"):
            response = bridge.app.test_client().get("/captcha-status")
        self.assertIn("no-store", response.headers["Cache-Control"])


if __name__ == "__main__":
    unittest.main()
