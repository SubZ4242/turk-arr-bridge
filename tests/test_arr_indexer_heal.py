import os
import unittest
from unittest.mock import patch


os.environ.setdefault("CONFIG_FILE", "/tmp/turk-arr-bridge-test-config.json")

import bridge  # noqa: E402


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, content=b""):
        self.status_code = status_code
        self._json_data = json_data
        self.content = content

    @property
    def ok(self):
        return 200 <= self.status_code < 400

    def json(self):
        return self._json_data

    def raise_for_status(self):
        if not self.ok:
            raise bridge.requests.HTTPError(f"HTTP {self.status_code}")


def bridge_indexer(base_url="http://nas.local:9696/torznab"):
    return {
        "id": 2,
        "name": "Turk ARR Bridge",
        "implementation": "Torznab",
        "configContract": "TorznabSettings",
        "enableRss": True,
        "enableAutomaticSearch": True,
        "enableInteractiveSearch": True,
        "fields": [
            {"name": "baseUrl", "value": base_url},
            {"name": "apiPath", "value": "/api"},
        ],
    }


class ArrIndexerSelfHealingTests(unittest.TestCase):
    def setUp(self):
        bridge._config["arr_indexer_auto_heal"] = True
        bridge._config["bridge_port"] = 9696
        bridge._config["bridge_external_url"] = ""
        bridge.SONARR_URL = "http://sonarr.test"
        bridge.SONARR_API_KEY = "sonarr-key"
        bridge.RADARR_URL = ""
        bridge.RADARR_API_KEY = ""

    def test_recognizes_only_torznab_indexer_pointing_to_bridge(self):
        self.assertTrue(bridge._is_this_bridge_indexer(bridge_indexer()))
        self.assertFalse(
            bridge._is_this_bridge_indexer(
                bridge_indexer("http://nas.local:30196/api/v2.0/indexers/turktorrent")
            )
        )
        non_torznab = bridge_indexer()
        non_torznab["implementation"] = "Newznab"
        non_torznab["configContract"] = "NewznabSettings"
        self.assertFalse(bridge._is_this_bridge_indexer(non_torznab))

    def test_does_not_reset_arr_when_tracker_probe_fails(self):
        with patch.object(
            bridge, "_probe_upstream_torznab", return_value=(False, "login failed")
        ), patch.object(bridge.requests, "post") as post:
            result = bridge.heal_arr_indexers()

        self.assertFalse(result["upstream_ok"])
        self.assertEqual(result["tested"], 0)
        self.assertIn("login failed", result["error"])
        post.assert_not_called()

    def test_retests_bridge_and_clears_persistent_sonarr_backoff(self):
        indexer_response = FakeResponse(json_data=[bridge_indexer()])
        test_response = FakeResponse(status_code=200, json_data={})
        with patch.object(
            bridge, "_probe_upstream_torznab", return_value=(True, "")
        ), patch.object(
            bridge, "_arr_has_indexer_warning", return_value=True
        ), patch.object(
            bridge.requests, "get", return_value=indexer_response
        ), patch.object(
            bridge.requests, "post", return_value=test_response
        ) as post:
            result = bridge.heal_arr_indexers()

        self.assertTrue(result["upstream_ok"])
        self.assertEqual(result["tested"], 1)
        self.assertEqual(result["recovered"], 1)
        self.assertEqual(result["error"], "")
        self.assertEqual(post.call_count, 1)
        args, kwargs = post.call_args
        self.assertEqual(args[0], "http://sonarr.test/api/v3/indexer/test")
        self.assertEqual(kwargs["headers"]["X-Api-Key"], "sonarr-key")
        self.assertEqual(kwargs["json"]["id"], 2)

    def test_probe_rejects_torznab_error_xml_even_with_http_200(self):
        bridge.UPSTREAM_TORZNAB_URL = "http://jackett.test/torznab"
        bridge.JACKETT_API_KEY = "jackett-key"
        response = FakeResponse(
            status_code=200,
            content=b'<error code="900" description="Cookie expired" />',
        )
        with patch.object(bridge.requests, "get", return_value=response):
            ok, error = bridge._probe_upstream_torznab()

        self.assertFalse(ok)
        self.assertEqual(error, "Cookie expired")


if __name__ == "__main__":
    unittest.main()
