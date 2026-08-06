import os
import unittest
from unittest.mock import Mock, patch


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

    def test_session_alert_link_autostarts_one_captcha_request(self):
        with bridge.app.test_client() as client:
            response = client.get("/captcha?autostart=1&request=123")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"setTimeout(requestNewCaptcha, 250)", response.data)

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


class TurkTorrentCookiePersistenceTests(unittest.TestCase):
    @staticmethod
    def _flaresolverr_response(page_html):
        response = Mock()
        response.ok = True
        response.json.return_value = {
            "status": "ok",
            "solution": {"response": page_html},
        }
        return response

    @patch("bridge.requests.post")
    def test_explicit_tsue_guest_response_expires_cookie(self, post):
        post.return_value = self._flaresolverr_response(
            '<script>var TSUE = {memberid: "0", membername: "Guest"};</script>'
        )

        result = bridge._validate_turktorrent_cookie(
            "tsue_member=remember-me-token",
            "https://turktorrent.us",
            "http://flaresolverr:8191",
        )

        self.assertFalse(result["ok"])
        self.assertIn("memberid=0", result["error"])
        sent_cookie = post.call_args.kwargs["json"]["cookies"][0]
        self.assertEqual(sent_cookie["name"], "tsue_member")
        self.assertEqual(sent_cookie["value"], "remember-me-token")

    @patch("bridge.requests.post")
    def test_authenticated_tsue_response_keeps_cookie(self, post):
        post.return_value = self._flaresolverr_response(
            '<script>var TSUE = {memberid: "42", membername: "Halil"};</script>'
        )

        result = bridge._validate_turktorrent_cookie(
            "tsue_member=remember-me-token",
            "https://turktorrent.us",
            "http://flaresolverr:8191",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["error"], "")

    @patch("bridge.requests.post", side_effect=TimeoutError("temporary outage"))
    def test_probe_failure_never_expires_saved_cookie(self, _post):
        result = bridge._validate_turktorrent_cookie(
            "tsue_member=remember-me-token",
            "https://turktorrent.us",
            "http://flaresolverr:8191",
        )

        self.assertTrue(result["ok"])
        self.assertIn("bleibt erhalten", result["error"])

    @patch("bridge.requests.post")
    def test_ambiguous_html_never_expires_saved_cookie(self, post):
        post.return_value = self._flaresolverr_response(
            '<html><form id="loginbox_form"></form></html>'
        )

        result = bridge._validate_turktorrent_cookie(
            "tsue_member=remember-me-token",
            "https://turktorrent.us",
            "http://flaresolverr:8191",
        )

        self.assertTrue(result["ok"])
        self.assertIn("bleibt erhalten", result["error"])

    def test_negative_jackett_test_requests_new_login(self):
        saved = {
            key: bridge._config.get(key)
            for key in ("jackett_url", "jackett_api_key", "jackett_admin_password")
        }
        bridge._config.update({
            "jackett_url": "http://jackett.test:9117",
            "jackett_api_key": "api-key",
            "jackett_admin_password": "",
        })
        response = Mock(ok=True)
        response.json.return_value = {
            "Results": [],
            "Indexers": [{"Error": "Login failed: selector did not match"}],
        }
        session = Mock()
        session.get.return_value = response
        try:
            with patch.object(bridge, "_get_jackett_session", return_value=session), patch(
                "bridge.requests.post"
            ) as flaresolverr_post:
                result = bridge._validate_turktorrent_cookie(
                    "tsue_member=remember-me-token",
                    "https://turktorrent.us",
                    "http://flaresolverr:8191",
                )
        finally:
            for key, value in saved.items():
                if value is None:
                    bridge._config.pop(key, None)
                else:
                    bridge._config[key] = value

        self.assertFalse(result["ok"])
        self.assertIn("Jackett-Test negativ", result["error"])
        flaresolverr_post.assert_not_called()


class TelegramCaptchaCleanupTests(unittest.TestCase):
    def test_new_captcha_replaces_previous_request_and_persists_message_id(self):
        old_id = bridge._config.get("telegram_last_captcha_message_id")
        bridge._config["telegram_last_captcha_message_id"] = "100"
        try:
            with patch.object(bridge, "_delete_captcha_telegram_alert") as delete, patch.object(
                bridge, "_send_telegram_alert", return_value=101
            ) as send, patch.object(bridge, "_save_config") as save:
                result = bridge._send_captcha_telegram_alert("new captcha")

            delete.assert_called_once_with()
            send.assert_called_once_with("new captcha")
            save.assert_called_once()
            self.assertEqual(result, 101)
            self.assertEqual(bridge._config["telegram_last_captcha_message_id"], "101")
        finally:
            if old_id is None:
                bridge._config.pop("telegram_last_captcha_message_id", None)
            else:
                bridge._config["telegram_last_captcha_message_id"] = old_id

    def test_session_expired_alert_is_sent_once_and_pinned(self):
        saved = {
            key: bridge._config.get(key)
            for key in (
                "telegram_session_expired_message_id",
                "telegram_bot_token",
                "telegram_chat_id",
                "bridge_external_url",
            )
        }
        bridge._config.update({
            "telegram_session_expired_message_id": "",
            "telegram_bot_token": "bot-token",
            "telegram_chat_id": "1234",
            "bridge_external_url": "http://100.79.56.23:9696",
        })
        pin_response = Mock(ok=True)
        try:
            with patch.object(bridge, "_delete_captcha_telegram_alert") as delete_captcha, patch.object(
                bridge, "_send_telegram_alert", return_value=202
            ) as send, patch.object(
                bridge, "_get_internal_bridge_url", return_value="http://192.168.1.50:9696"
            ), patch.object(
                bridge.requests, "post", return_value=pin_response
            ) as telegram_post, patch.object(bridge, "_save_config"):
                first = bridge._send_session_expired_alert("login failed")
                second = bridge._send_session_expired_alert("login failed again")

            self.assertEqual(first, 202)
            self.assertEqual(second, "202")
            delete_captcha.assert_called_once_with()
            send.assert_called_once()
            message = send.call_args.args[0]
            self.assertIn("Session abgelaufen", message)
            self.assertIn("autostart=1", message)
            self.assertIn("100.79.56.23", message)
            telegram_post.assert_called_once()
            self.assertTrue(telegram_post.call_args.args[0].endswith("/pinChatMessage"))
            self.assertFalse(telegram_post.call_args.kwargs["json"]["disable_notification"])
        finally:
            for key, value in saved.items():
                if value is None:
                    bridge._config.pop(key, None)
                else:
                    bridge._config[key] = value

    def test_successful_login_unpins_and_deletes_session_alert(self):
        saved = {
            key: bridge._config.get(key)
            for key in (
                "telegram_session_expired_message_id",
                "telegram_bot_token",
                "telegram_chat_id",
            )
        }
        bridge._config.update({
            "telegram_session_expired_message_id": "303",
            "telegram_bot_token": "bot-token",
            "telegram_chat_id": "1234",
        })
        response = Mock(ok=True)
        try:
            with patch.object(bridge.requests, "post", return_value=response) as post, patch.object(
                bridge, "_save_config"
            ) as save:
                bridge._clear_session_expired_alert()

            self.assertEqual(post.call_count, 2)
            self.assertTrue(post.call_args_list[0].args[0].endswith("/unpinChatMessage"))
            self.assertTrue(post.call_args_list[1].args[0].endswith("/deleteMessage"))
            self.assertEqual(bridge._config["telegram_session_expired_message_id"], "")
            save.assert_called_once()
        finally:
            for key, value in saved.items():
                if value is None:
                    bridge._config.pop(key, None)
                else:
                    bridge._config[key] = value


if __name__ == "__main__":
    unittest.main()
