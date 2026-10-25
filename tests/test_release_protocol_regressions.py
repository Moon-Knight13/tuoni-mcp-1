from tuoni_mcp.protocol import error_result, redact_text


def test_quoted_secret_assignments_redact_escaped_quotes_and_backslashes():
    for text in (
        r'{"password": "prefix\"private-tail", "host":"visible"}',
        r"password='prefix\'private-tail' host=visible",
        r'{"token": "prefix\\private-tail", "host":"visible"}',
    ):
        redacted = redact_text(text)
        assert "private-tail" not in redacted
        assert "visible" in redacted
        assert redact_text(redacted) == redacted


def test_tiny_private_error_budgets_terminate_with_the_fixed_fallback():
    response = error_result("E", "x", budget=1)
    assert response.isError
    assert '"code":"ERROR"' in response.content[0].text
