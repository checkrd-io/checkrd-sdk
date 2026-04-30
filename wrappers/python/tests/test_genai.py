"""Tests for the OpenTelemetry GenAI semconv extractor."""

from __future__ import annotations

import pytest

from checkrd._genai import (
    attributes_for_url,
    detect_operation,
    detect_provider,
)


class TestDetectProvider:
    @pytest.mark.parametrize(
        "host,expected",
        [
            ("api.openai.com", "openai"),
            ("api.anthropic.com", "anthropic"),
            ("api.cohere.com", "cohere"),
            ("api.cohere.ai", "cohere"),
            ("api.groq.com", "groq"),
            ("api.mistral.ai", "mistral.ai"),
            ("api.together.xyz", "together.ai"),
            ("api.together.ai", "together.ai"),
            ("generativelanguage.googleapis.com", "google.gemini"),
            ("api.perplexity.ai", "perplexity"),
            ("api.fireworks.ai", "fireworks"),
            ("api.x.ai", "xai"),
        ],
    )
    def test_known_providers(self, host: str, expected: str) -> None:
        assert detect_provider(host) == expected

    @pytest.mark.parametrize(
        "host,expected",
        [
            ("bedrock-runtime.us-east-1.amazonaws.com", "aws.bedrock"),
            ("bedrock-runtime.eu-west-1.amazonaws.com", "aws.bedrock"),
            ("bedrock.us-west-2.amazonaws.com", "aws.bedrock"),
            ("my-deployment.openai.azure.com", "azure.openai"),
            ("us-central1-aiplatform.googleapis.com", "google.vertex_ai"),
        ],
    )
    def test_suffix_providers(self, host: str, expected: str) -> None:
        assert detect_provider(host) == expected

    def test_case_insensitive(self) -> None:
        assert detect_provider("API.OPENAI.COM") == "openai"

    def test_unknown_returns_none(self) -> None:
        assert detect_provider("api.example.com") is None

    def test_empty_returns_none(self) -> None:
        assert detect_provider("") is None


class TestDetectOperation:
    @pytest.mark.parametrize(
        "path,expected",
        [
            ("/v1/chat/completions", "chat"),
            ("/v1/messages", "chat"),
            ("/v1/converse", "chat"),
            ("/v1/responses", "chat"),
            ("/v1/embeddings", "embeddings"),
            ("/v1/embed", "embeddings"),
            ("/v1/completions", "text_completion"),
            ("/v1beta/models/gemini-pro:generateContent", "generate_content"),
            ("/v1beta/models/gemini-pro:streamGenerateContent", "generate_content"),
            ("/v1/images/generations", "image.generation"),
            ("/v1/audio/speech", "audio.speech"),
            ("/v1/audio/transcriptions", "audio.transcription"),
        ],
    )
    def test_known_operations(self, path: str, expected: str) -> None:
        assert detect_operation(path) == expected

    def test_chat_completions_resolves_chat_not_text_completion(self) -> None:
        # The substring ``/completions`` would match the
        # ``text_completion`` rule if order were wrong; the more
        # specific ``/chat/completions`` rule must win.
        assert detect_operation("/v1/chat/completions") == "chat"

    def test_unknown_returns_none(self) -> None:
        assert detect_operation("/v1/random/path") is None


class TestAttributesForUrl:
    def test_openai_chat(self) -> None:
        attrs = attributes_for_url("api.openai.com", "/v1/chat/completions")
        assert attrs == {
            "gen_ai.provider.name": "openai",
            "gen_ai.operation.name": "chat",
        }

    def test_anthropic_messages(self) -> None:
        attrs = attributes_for_url("api.anthropic.com", "/v1/messages")
        assert attrs == {
            "gen_ai.provider.name": "anthropic",
            "gen_ai.operation.name": "chat",
        }

    def test_bedrock_converse(self) -> None:
        attrs = attributes_for_url(
            "bedrock-runtime.us-east-1.amazonaws.com",
            "/model/anthropic.claude-3-haiku-20240307-v1:0/converse",
        )
        assert attrs == {
            "gen_ai.provider.name": "aws.bedrock",
            "gen_ai.operation.name": "chat",
        }

    def test_unknown_url_returns_empty_dict(self) -> None:
        # Empty dict means "no GenAI inferred" — caller does not stamp
        # placeholder ``"unknown"`` values.
        assert attributes_for_url("api.example.com", "/random") == {}

    def test_provider_match_only_when_path_unmapped(self) -> None:
        attrs = attributes_for_url("api.openai.com", "/v1/files")
        assert attrs == {"gen_ai.provider.name": "openai"}

    def test_path_match_only_when_provider_unmapped(self) -> None:
        attrs = attributes_for_url("internal-proxy.example.com", "/chat/completions")
        assert attrs == {"gen_ai.operation.name": "chat"}
