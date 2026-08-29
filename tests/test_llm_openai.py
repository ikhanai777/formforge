"""The OpenAI-compatible client, which is the free-model path.

Everything here runs against a stub server rather than a real one. What is
being tested is the translation in both directions -- Anthropic's cached system
blocks and image blocks going out, an OpenAI choices payload coming back --
because that translation is the whole client, and getting it wrong fails at the
far end where the error is unhelpful.
"""

from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar

import pytest

from formforge.llm import LLMError, OpenAICompatibleClient, Tier, build_client

RECEIVED: list[dict] = []


class _Handler(BaseHTTPRequestHandler):
    reply: ClassVar[dict] = {}
    status: ClassVar[int] = 200

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        RECEIVED.append(json.loads(self.rfile.read(length)))
        body = json.dumps(self.reply).encode()
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return


@pytest.fixture
def server():
    RECEIVED.clear()
    _Handler.status = 200
    _Handler.reply = {
        "choices": [{"message": {"content": "built it"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 34},
    }
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}/v1"
    httpd.shutdown()


class TestRoundTrip:
    def test_a_completion_comes_back_normalised(self, server):
        client = OpenAICompatibleClient(base_url=server, models=dict.fromkeys(Tier, "m"))
        out = client.complete(system="rules", messages=[{"role": "user", "content": "hi"}])
        assert out.text == "built it"
        assert out.usage.input_tokens == 120
        assert out.usage.output_tokens == 34
        assert out.model == "m"

    def test_a_local_model_is_priced_at_zero(self, server):
        """Not unknown, zero. A model on your own hardware has no per-token
        price, and the stats table should not imply one."""
        client = OpenAICompatibleClient(base_url=server, models=dict.fromkeys(Tier, "m"))
        out = client.complete(system="", messages=[{"role": "user", "content": "hi"}])
        assert out.usage.cost_usd == 0.0


class TestTranslation:
    def test_cached_system_blocks_collapse_to_one_message(self, server):
        client = OpenAICompatibleClient(base_url=server, models=dict.fromkeys(Tier, "m"))
        client.complete(
            system=[
                {"type": "text", "text": "DFM rules"},
                {"type": "text", "text": "cheatsheet", "cache_control": {"type": "ephemeral"}},
            ],
            messages=[{"role": "user", "content": "hi"}],
        )
        sent = RECEIVED[-1]["messages"]
        assert sent[0]["role"] == "system"
        assert sent[0]["content"] == "DFM rules\n\ncheatsheet"
        # cache_control has no counterpart on the far side; it must not survive
        # into the payload as a stray key.
        assert "cache_control" not in json.dumps(sent)

    def test_images_become_data_urls_so_critique_can_work(self, server):
        """The critique step sends preview renders. Dropping them would leave a
        vision-capable local model looking at nothing."""
        client = OpenAICompatibleClient(base_url=server, models=dict.fromkeys(Tier, "m"))
        pixel = base64.b64encode(b"\x89PNG fake").decode()
        client.complete(
            system="",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "does this match?"},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": pixel,
                            },
                        },
                    ],
                }
            ],
        )
        content = RECEIVED[-1]["messages"][-1]["content"]
        assert isinstance(content, list)
        kinds = [part["type"] for part in content]
        assert kinds == ["text", "image_url"]
        assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")

    def test_a_lone_text_block_stays_a_plain_string(self, server):
        """Servers that only accept string content should not be handed a list
        for a message that never needed one."""
        client = OpenAICompatibleClient(base_url=server, models=dict.fromkeys(Tier, "m"))
        client.complete(
            system="",
            messages=[{"role": "user", "content": [{"type": "text", "text": "plain"}]}],
        )
        assert RECEIVED[-1]["messages"][-1]["content"] == "plain"


class TestFailures:
    def test_an_unreachable_server_says_where_to_look(self):
        client = OpenAICompatibleClient(
            base_url="http://127.0.0.1:9/v1", models=dict.fromkeys(Tier, "m"), timeout_s=2
        )
        with pytest.raises(LLMError) as excinfo:
            client.complete(system="", messages=[{"role": "user", "content": "hi"}])
        message = str(excinfo.value)
        assert "could not reach a model server" in message
        assert "FORMFORGE_LLM_BASE_URL" in message

    def test_a_server_error_is_retryable(self, server):
        _Handler.status = 503
        _Handler.reply = {"error": "overloaded"}
        client = OpenAICompatibleClient(base_url=server, models=dict.fromkeys(Tier, "m"))
        with pytest.raises(LLMError) as excinfo:
            client.complete(system="", messages=[{"role": "user", "content": "hi"}])
        assert excinfo.value.retryable


class TestSelection:
    def test_a_base_url_selects_the_local_backend(self, monkeypatch):
        """Setting a base URL is the user pointing us at their own model; there
        is no other reason to set it."""
        monkeypatch.setenv("FORMFORGE_LLM_BASE_URL", "http://ollama:11434/v1")
        monkeypatch.delenv("FORMFORGE_LLM_BACKEND", raising=False)
        monkeypatch.delenv("FORMFORGE_OFFLINE", raising=False)
        assert isinstance(build_client(), OpenAICompatibleClient)

    def test_offline_still_wins_when_asked(self, monkeypatch):
        monkeypatch.setenv("FORMFORGE_LLM_BASE_URL", "http://ollama:11434/v1")
        monkeypatch.setenv("FORMFORGE_OFFLINE", "1")
        assert not isinstance(build_client(), OpenAICompatibleClient)
