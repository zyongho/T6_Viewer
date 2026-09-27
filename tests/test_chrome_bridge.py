from tesla_viewer.chrome_bridge import extract_bearer, is_receive_timeout


def test_extract_bearer_header_case_insensitive():
    assert extract_bearer({"authorization": "Bearer abc123"}) == "abc123"
    assert extract_bearer({"Authorization": "BEARER xyz"}) == "xyz"


def test_extract_bearer_rejects_non_bearer_values():
    assert extract_bearer({"Authorization": "Basic abc"}) is None
    assert extract_bearer({"Content-Type": "application/json"}) is None


def test_receive_timeout_is_not_connection_loss():
    assert is_receive_timeout(TimeoutError("Connection timed out"))
