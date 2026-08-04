import os
import unittest
from unittest.mock import patch


os.environ.setdefault("CONFIG_FILE", "/tmp/turk-arr-bridge-test-config.json")

import bridge  # noqa: E402


class FakeResponse:
    def __init__(self, status_code=200, text="", content=b"", headers=None):
        self.status_code = status_code
        self.text = text
        self.content = content
        self.headers = headers or {}

    @property
    def ok(self):
        return 200 <= self.status_code < 400


class FakeSession:
    def __init__(self, login=None, version=None, add=None):
        self.login_response = login or FakeResponse(204)
        self.version_response = version or FakeResponse(200, "v5.2.3")
        self.add_response = add or FakeResponse(204)
        self.headers = {}
        self.cookies = {"QBT_SID_8080": "dynamic-cookie-name"}
        self.calls = []
        self.closed = False

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.version_response

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        if url.endswith("/api/v2/auth/login"):
            return self.login_response
        if url.endswith("/api/v2/torrents/add"):
            return self.add_response
        raise AssertionError(f"Unexpected POST {url}")

    def close(self):
        self.closed = True


class QBittorrentCompatibilityTests(unittest.TestCase):
    def setUp(self):
        bridge._qbit_invalidate_session()
        bridge.QBIT_URL = "http://qbit.test:8080"
        bridge.QBIT_USER = "user"
        bridge.QBIT_PASS = "secret"

    def tearDown(self):
        bridge._qbit_invalidate_session()

    def test_qbittorrent_5_2_accepts_204_and_dynamic_cookie_name(self):
        fake = FakeSession(
            login=FakeResponse(204, ""),
            version=FakeResponse(200, "v5.2.3"),
        )
        with patch.object(bridge.requests, "Session", return_value=fake):
            session, version, error = bridge.qbit_connect(force_login=True)

        self.assertIs(session, fake)
        self.assertEqual(version, "v5.2.3")
        self.assertEqual(error, "")
        self.assertEqual(fake.headers["Origin"], "http://qbit.test:8080")
        self.assertNotIn("SID", fake.cookies)

    def test_legacy_fails_body_is_still_rejected(self):
        fake = FakeSession(login=FakeResponse(200, "Fails."))
        with patch.object(bridge.requests, "Session", return_value=fake):
            session, version, error = bridge.qbit_connect(force_login=True)

        self.assertIsNone(session)
        self.assertEqual(version, "")
        self.assertIn("Login fehlgeschlagen", error)
        self.assertFalse(any(call[0] == "GET" for call in fake.calls))

    def test_torrent_add_accepts_new_204_empty_response(self):
        fake = FakeSession(add=FakeResponse(204, ""))
        with patch.object(bridge, "qbit_login", return_value=fake):
            result = bridge.qbit_add_torrent(
                "magnet:?xt=urn:btih:0123456789abcdef",
                category="tv-tr-boxset",
            )

        self.assertTrue(result)
        add_calls = [call for call in fake.calls if call[1].endswith("/torrents/add")]
        self.assertEqual(len(add_calls), 1)
        self.assertIn("magnet:?xt=", add_calls[0][2]["data"]["urls"])

    def test_torrent_add_keeps_legacy_ok_response_compatible(self):
        fake = FakeSession(add=FakeResponse(200, "Ok."))
        with patch.object(bridge, "qbit_login", return_value=fake):
            result = bridge.qbit_add_torrent(
                "magnet:?xt=urn:btih:fedcba9876543210"
            )

        self.assertTrue(result)


if __name__ == "__main__":
    unittest.main()
