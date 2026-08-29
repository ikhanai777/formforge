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

    get_reply: ClassVar[dict] = {}

    def do_GET(self):
        body = json.dumps(self.get_reply).encode()
        self.send_response(200)
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


class TestModelDiscovery:
    """Hardcoding model IDs is how a working config rots.

    A free tier's lineup changes without notice, and a retired ID fails at
    request time rather than at configuration time -- which is the reason
    llm.py resolves Anthropic's IDs rather than trusting them too.
    """

    def test_openrouter_pricing_marks_the_free_models(self, server):
        _Handler.get_reply = {
            "data": [
                {
                    "id": "vendor/big-model",
                    "pricing": {"prompt": "0.000002", "completion": "0.000006"},
                    "architecture": {"input_modalities": ["text"]},
                    "context_length": 128000,
                },
                {
                    "id": "vendor/small-model:free",
                    "pricing": {"prompt": "0", "completion": "0"},
                    "architecture": {"input_modalities": ["text", "image"]},
                    "context_length": 64000,
                },
            ]
        }
        found = OpenAICompatibleClient(base_url=server).list_models()
        by_id = {m["id"]: m for m in found}
        assert by_id["vendor/small-model:free"]["free"] is True
        assert by_id["vendor/big-model"]["free"] is False
        # Free models sort first, because that is what the flag is for.
        assert found[0]["id"] == "vendor/small-model:free"

    def test_vision_is_read_from_the_modalities(self, server):
        _Handler.get_reply = {
            "data": [
                {
                    "id": "sees/things",
                    "pricing": {"prompt": "0", "completion": "0"},
                    "architecture": {"input_modalities": ["text", "image"]},
                },
                {
                    "id": "reads/only",
                    "pricing": {"prompt": "0", "completion": "0"},
                    "architecture": {"input_modalities": ["text"]},
                },
            ]
        }
        by_id = {m["id"]: m for m in OpenAICompatibleClient(base_url=server).list_models()}
        assert by_id["sees/things"]["vision"] is True
        assert by_id["reads/only"]["vision"] is False

    def test_a_bare_openai_listing_still_works(self, server):
        """Ollama and llama.cpp return ids and nothing else. The `:free` suffix
        is only consulted when there is no pricing to read, because the price
        is the fact and the suffix is a convention."""
        _Handler.get_reply = {
            "data": [{"id": "qwen2.5-coder:7b", "object": "model"}]
        }
        found = OpenAICompatibleClient(base_url=server).list_models()
        assert found == [
            {"id": "qwen2.5-coder:7b", "free": False, "vision": False, "context": None}
        ]

    def test_a_priced_model_is_not_free_whatever_its_name_says(self, server):
        _Handler.get_reply = {
            "data": [{"id": "vendor/looks-free:free", "pricing": {"prompt": "0.001"}}]
        }
        assert OpenAICompatibleClient(base_url=server).list_models()[0]["free"] is False


class TestAttribution:
    def test_headers_are_sent_only_when_configured(self, server, monkeypatch):
        """OpenRouter asks for these; a local server never asked for anything."""
        plain = OpenAICompatibleClient(base_url=server)
        assert "HTTP-Referer" not in plain._headers()
        monkeypatch.setenv("FORMFORGE_LLM_REFERER", "https://example.test")
        monkeypatch.setenv("FORMFORGE_LLM_TITLE", "FormForge")
        tagged = OpenAICompatibleClient(base_url=server)
        assert tagged._headers()["HTTP-Referer"] == "https://example.test"
        assert tagged._headers()["X-Title"] == "FormForge"
