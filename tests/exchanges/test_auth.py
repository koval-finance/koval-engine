from koval.exchanges.auth import redact_mapping, redact_secret, safe_exception_message


def test_redact_secret_returns_stable_mask_not_original():
    first = redact_secret("abcd1234")
    second = redact_secret("abcd1234")

    assert first == second
    assert first == "<redacted>"
    assert "abcd" not in first


def test_redact_mapping_redacts_sensitive_keys_recursively_without_mutation():
    source = {
        "api_key": "key-123",
        "nested": {
            "X-MBX-APIKEY": "binance-key",
            "items": [{"signature": "sig-123"}, {"safe": "visible"}],
        },
        "X-TXC-PAYLOAD": "payload",
        "X-TXC-SIGNATURE": "signature",
    }

    redacted = redact_mapping(source)

    assert redacted["api_key"] != "key-123"
    assert redacted["nested"]["X-MBX-APIKEY"] != "binance-key"
    assert redacted["nested"]["items"][0]["signature"] != "sig-123"
    assert redacted["nested"]["items"][1]["safe"] == "visible"
    assert redacted["X-TXC-PAYLOAD"] != "payload"
    assert source["api_key"] == "key-123"


def test_redaction_passes_through_scalar_non_secrets():
    assert redact_mapping(None) is None
    assert redact_mapping(1) == 1
    assert redact_mapping(True) is True
    assert redact_mapping(["safe", 2]) == ["safe", 2]


def test_safe_exception_message_removes_credential_assignments():
    message = safe_exception_message(
        RuntimeError(
            "request failed: "
            "https://example.test/order?timestamp=1&signature=sig-123 "
            "api_key=key-123 api_secret=secret-123"
        )
    )

    assert "signature" not in message.lower()
    assert "api_key" not in message.lower()
    assert "api_secret" not in message.lower()
    assert "sig-123" not in message
    assert "key-123" not in message
    assert "secret-123" not in message
    assert "request failed" in message
