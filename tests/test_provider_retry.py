from clk_harness.orchestration.agent import AgentRunner
from clk_harness.tui import DashboardState


def retry_classifier(error: str) -> bool:
    runner = AgentRunner.__new__(AgentRunner)
    return runner._should_retry_provider(error)


def resolution_message(error: str) -> str:
    state = DashboardState.__new__(DashboardState)
    return state._provider_resolution_message(error)


def test_openrouter_no_endpoints_error_is_retryable() -> None:
    error = (
        "404 No endpoints available matching your guardrail restrictions "
        "and data policy. Configure: https://openrouter.ai/settings/privacy"
    )

    assert retry_classifier(error)


def test_auth_errors_remain_non_retryable() -> None:
    assert not retry_classifier("authentication failed: invalid API key")
    assert not retry_classifier("forbidden: account does not have access")


def test_no_endpoints_resolution_mentions_retry_and_backoff() -> None:
    message = resolution_message("No endpoints available matching your guardrail restrictions")

    assert "retries" in message
    assert "backoff" in message
