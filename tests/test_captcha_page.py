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
        self.old_external_url = bridge._config.get("bridge_external_url")

    def tearDown(self):
        bridge._captcha_request_active = self.old_active
        bridge._pending_captcha_sitekey = self.old_sitekey
        bridge._pending_captcha_host = self.old_host
        if self.old_external_url is None:
            bridge._config.pop("bridge_external_url", None)
        else:
            bridge._config["bridge_external_url"] = self.old_external_url

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

    def test_captcha_link_settings_are_in_telegram_panel_not_dashboard(self):
        html = bridge.GUI_HTML
        dashboard_start = html.index('id="panel-dashboard"')
        telegram_start = html.index('id="panel-notif-telegram"')
        captcha_settings = html.index("Telegram-Captcha-Zugriff")
        next_panel = html.index('id="panel-search"')

        self.assertFalse(dashboard_start < captcha_settings < telegram_start)
        self.assertTrue(telegram_start < captcha_settings < next_panel)

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

    def test_telegram_message_contains_internal_and_optional_external_links(self):
        bridge._config["bridge_external_url"] = "http://100.79.56.23:9696/"
        with patch.object(
            bridge, "_get_internal_bridge_url", return_value="http://192.168.1.50:9696"
        ), patch.object(
            bridge, "_send_telegram_alert"
        ) as send, patch.object(
            bridge._pending_captcha_event, "wait", return_value=False
        ), patch.object(
            bridge.time, "time", return_value=1234567890
        ):
            bridge._request_manual_captcha("https://turktorrent.us", timeout_minutes=1)

        first_message = send.call_args_list[0].args[0]
        self.assertIn("Intern / LAN öffnen", first_message)
        self.assertIn(
            "http://192.168.1.50:9696/captcha?request=1234567890",
            first_message,
        )
        self.assertIn("Extern / Tailscale öffnen", first_message)
        self.assertIn(
            "http://100.79.56.23:9696/captcha?request=1234567890",
            first_message,
        )

    def test_invalid_external_url_is_ignored(self):
        self.assertEqual(bridge._normalize_bridge_url("javascript:alert(1)"), "")
        self.assertEqual(bridge._normalize_bridge_url("100.79.56.23:9696"), "")
        self.assertEqual(
            bridge._normalize_bridge_url("https://nas.example.ts.net/"),
            "https://nas.example.ts.net",
        )

    def test_installations_without_external_url_keep_single_lan_link(self):
        bridge._config["bridge_external_url"] = ""
        with patch.object(
            bridge, "_get_internal_bridge_url", return_value="http://192.168.1.50:9696"
        ), patch.object(
            bridge, "_send_telegram_alert"
        ) as send, patch.object(
            bridge._pending_captcha_event, "wait", return_value=False
        ), patch.object(
            bridge.time, "time", return_value=1234567890
        ):
            bridge._request_manual_captcha("https://turktorrent.us", timeout_minutes=1)

        first_message = send.call_args_list[0].args[0]
        self.assertIn("Intern / LAN öffnen", first_message)
        self.assertNotIn("Extern / Tailscale öffnen", first_message)

    def test_forced_captcha_test_does_not_stop_at_valid_cookie(self):
        saved = {
            key: bridge._config.get(key)
            for key in (
                "turktorrent_username",
                "turktorrent_password",
                "flaresolverr_url",
                "turktorrent_current_cookie",
            )
        }
        bridge._config.update({
            "turktorrent_username": "user",
            "turktorrent_password": "secret",
            "flaresolverr_url": "http://flaresolverr.test",
            "turktorrent_current_cookie": "still-valid-cookie",
        })
        try:
            with patch.object(bridge, "_validate_turktorrent_cookie") as validate, patch.object(
                bridge,
                "_turktorrent_login",
                return_value={
                    "ok": False,
                    "error": "test stopped after login was reached",
                    "already_running": True,
                },
            ) as login:
                result = bridge._do_cookie_refresh(force_login=True)
        finally:
            for key, value in saved.items():
                if value is None:
                    bridge._config.pop(key, None)
                else:
                    bridge._config[key] = value

        validate.assert_not_called()
        login.assert_called_once()
        self.assertIn("test stopped", result["error"])


if __name__ == "__main__":
    unittest.main()
