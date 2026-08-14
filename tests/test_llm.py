"""LLM client health checks and response validation."""

from dataclasses import replace

from nlab_pipeline.config import settings
from nlab_pipeline.llm import LLMClient


class Response:
    def __init__(self, status: int) -> None:
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(str(self.status_code))


class Session:
    def __init__(self, status: int) -> None:
        self.status = status
        self.urls: list[str] = []

    def get(self, url: str, timeout: int) -> Response:
        self.urls.append(url)
        assert timeout == 5
        return Response(self.status)


def test_ping_rejects_http_error_and_removes_only_v1_suffix():
    client = LLMClient(replace(settings, llm_base_url="http://example.test/service/v1"))
    session = Session(503)
    client._session = session
    assert client.ping() is False
    assert session.urls == ["http://example.test/service/health"]
