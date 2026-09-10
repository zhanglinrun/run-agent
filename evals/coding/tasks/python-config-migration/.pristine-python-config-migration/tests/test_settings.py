import client
import settings


def test_override_is_honoured() -> None:
    assert settings.resolve({"timeout_seconds": 5})["timeout_seconds"] == 5


def test_client_reports_the_resolved_timeout() -> None:
    assert client.build({"timeout_seconds": 5}) == "timeout=5s"
