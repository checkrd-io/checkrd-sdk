"""Tests for body-derived GenAI semconv extraction.

Verifies the four canonical vendor shapes (OpenAI request, OpenAI
response, Anthropic request, Anthropic response) plus the safety
properties (unknown provider returns empty, oversized body returns
empty, invalid JSON returns empty).
"""

from __future__ import annotations

import json

from checkrd._genai_body import extract_request_attrs, extract_response_attrs


# ---------------------------------------------------------------------------
# OpenAI shape
# ---------------------------------------------------------------------------


def test_openai_request_extracts_model_and_stream() -> None:
    body = json.dumps({
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
    }).encode("utf-8")
    assert extract_request_attrs("openai", body) == {
        "gen_ai.request.model": "gpt-4o-mini",
        "gen_ai.request.stream": True,
    }


def test_openai_request_handles_missing_stream() -> None:
    body = json.dumps({"model": "gpt-4o", "messages": []}).encode("utf-8")
    assert extract_request_attrs("openai", body) == {
        "gen_ai.request.model": "gpt-4o",
    }


def test_openai_response_extracts_usage() -> None:
    body = json.dumps({
        "id": "chatcmpl-...",
        "model": "gpt-4o-mini-2024-07-18",
        "choices": [],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 25,
            "total_tokens": 35,
        },
    }).encode("utf-8")
    assert extract_response_attrs("openai", body) == {
        "gen_ai.response.model": "gpt-4o-mini-2024-07-18",
        "gen_ai.usage.input_tokens": 10,
        "gen_ai.usage.output_tokens": 25,
    }


def test_azure_openai_uses_same_shape() -> None:
    """Azure OpenAI ships the OpenAI-compatible response — extractor
    must route both providers through the same code path."""
    body = json.dumps({
        "model": "gpt-4o-mini",
        "usage": {"prompt_tokens": 5, "completion_tokens": 10},
    }).encode("utf-8")
    assert extract_response_attrs("azure.openai", body) == {
        "gen_ai.response.model": "gpt-4o-mini",
        "gen_ai.usage.input_tokens": 5,
        "gen_ai.usage.output_tokens": 10,
    }


# ---------------------------------------------------------------------------
# Anthropic / Bedrock shape
# ---------------------------------------------------------------------------


def test_anthropic_request_extracts_model_and_stream() -> None:
    body = json.dumps({
        "model": "claude-3-haiku-20240307",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": False,
    }).encode("utf-8")
    assert extract_request_attrs("anthropic", body) == {
        "gen_ai.request.model": "claude-3-haiku-20240307",
        "gen_ai.request.stream": False,
    }


def test_anthropic_response_extracts_usage() -> None:
    body = json.dumps({
        "id": "msg_...",
        "type": "message",
        "model": "claude-3-haiku-20240307",
        "content": [],
        "usage": {"input_tokens": 12, "output_tokens": 38},
    }).encode("utf-8")
    assert extract_response_attrs("anthropic", body) == {
        "gen_ai.response.model": "claude-3-haiku-20240307",
        "gen_ai.usage.input_tokens": 12,
        "gen_ai.usage.output_tokens": 38,
    }


def test_aws_bedrock_uses_anthropic_shape() -> None:
    """Anthropic-on-Bedrock returns the same envelope — extractor
    routes ``aws.bedrock`` through the same code path."""
    body = json.dumps({
        "model": "anthropic.claude-3-haiku-20240307-v1:0",
        "usage": {"input_tokens": 7, "output_tokens": 14},
    }).encode("utf-8")
    assert extract_response_attrs("aws.bedrock", body) == {
        "gen_ai.response.model": "anthropic.claude-3-haiku-20240307-v1:0",
        "gen_ai.usage.input_tokens": 7,
        "gen_ai.usage.output_tokens": 14,
    }


# ---------------------------------------------------------------------------
# Safety properties
# ---------------------------------------------------------------------------


def test_unknown_provider_returns_empty() -> None:
    """No guessing across unrelated provider shapes — unknown returns empty."""
    body = json.dumps({"model": "gpt-4o", "usage": {"prompt_tokens": 5}}).encode()
    assert extract_request_attrs("perplexity", body) == {}
    assert extract_response_attrs("perplexity", body) == {}


def test_none_provider_returns_empty() -> None:
    body = json.dumps({"model": "gpt-4o"}).encode("utf-8")
    assert extract_request_attrs(None, body) == {}
    assert extract_response_attrs(None, body) == {}


def test_empty_body_returns_empty() -> None:
    assert extract_request_attrs("openai", b"") == {}
    assert extract_request_attrs("openai", None) == {}


def test_invalid_json_returns_empty() -> None:
    """Hostile or truncated JSON must not crash the telemetry path."""
    assert extract_request_attrs("openai", b"not json") == {}
    assert extract_response_attrs("openai", b"{incomplete") == {}


def test_invalid_utf8_returns_empty() -> None:
    """Binary content (e.g. an audio response routed through the same
    transport) must not crash the JSON parser."""
    assert extract_response_attrs("openai", b"\xff\xfe\xfd") == {}


def test_oversized_body_returns_empty() -> None:
    """Bodies above the 1 MB cap return empty rather than allocating
    memory to parse them."""
    huge = b"{" + b" " * (1_100_000) + b"}"
    assert extract_request_attrs("openai", huge) == {}


def test_non_object_root_returns_empty() -> None:
    """A JSON array at the root is not a vendor request shape."""
    assert extract_request_attrs("openai", b"[1, 2, 3]") == {}


def test_missing_fields_extract_partial() -> None:
    """A response with only ``model`` (no usage) returns just the model."""
    body = json.dumps({"model": "gpt-4o"}).encode("utf-8")
    assert extract_response_attrs("openai", body) == {
        "gen_ai.response.model": "gpt-4o",
    }


def test_wrong_type_fields_skipped() -> None:
    """``usage.prompt_tokens`` must be an int — string values are skipped."""
    body = json.dumps({
        "model": "gpt-4o",
        "usage": {"prompt_tokens": "ten", "completion_tokens": 5},
    }).encode("utf-8")
    attrs = extract_response_attrs("openai", body)
    # ``completion_tokens`` is valid; ``prompt_tokens`` is skipped.
    assert "gen_ai.usage.input_tokens" not in attrs
    assert attrs["gen_ai.usage.output_tokens"] == 5
