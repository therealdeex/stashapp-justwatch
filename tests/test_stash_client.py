"""Transport: auth header selection against realistic server envelopes."""

from __future__ import annotations

from justwatch.stash_client import StashClient


def connection(**overrides):
    base = {
        "Scheme": "http", "Host": "localhost", "Port": 9998,
        # Stash passes a Go http.Cookie: exported fields are Name and Value.
        "SessionCookie": {"Name": "session", "Value": "s3cret-session-token"},
        "Dir": "", "PluginDir": "",
    }
    base.update(overrides)
    return base


class TestCookieAuth:
    def test_server_cookie_envelope_produces_cookie_header(self):
        client = StashClient(connection())
        assert client.api_key == ""
        assert client._headers()["Cookie"] == "session=s3cret-session-token"

    def test_cookie_name_is_taken_from_the_envelope(self):
        client = StashClient(connection(
            SessionCookie={"Name": "stash-session", "Value": "tok"},
        ))
        assert client._headers()["Cookie"] == "stash-session=tok"

    def test_lowercase_keys_still_accepted(self):
        client = StashClient(connection(
            SessionCookie={"name": "session", "value": "legacy"},
        ))
        assert client._headers()["Cookie"] == "session=legacy"

    def test_no_cookie_and_no_key_means_anonymous_headers(self):
        client = StashClient(connection(SessionCookie=None))
        assert "Cookie" not in client._headers()
        assert "ApiKey" not in client._headers()

    def test_explicit_api_key_wins_over_cookie(self):
        client = StashClient(connection(), settings={"ApiKey": "key-1"})
        assert client._headers()["ApiKey"] == "key-1"
        assert "Cookie" not in client._headers()
