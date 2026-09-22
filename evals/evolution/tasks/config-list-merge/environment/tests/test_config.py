from config import resolve


def test_scalar_precedence_is_unchanged() -> None:
    assert resolve({"mode": "file"}, {"mode": "runtime"})["mode"] == "runtime"
