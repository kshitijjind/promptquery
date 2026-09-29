"""Tests for make_client provider inference (llm.make_client)."""
from __future__ import annotations

import pytest

from promptquery.llm import LLMError, make_client


def test_bare_o4_model_infers_openai(monkeypatch):
    pytest.importorskip("openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    client = make_client("o4-mini")
    assert client.name == "openai"
    assert client.model == "o4-mini"


def test_bare_gpt_model_infers_openai(monkeypatch):
    pytest.importorskip("openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    client = make_client("gpt-4o")
    assert client.name == "openai"
    assert client.model == "gpt-4o"


def test_unknown_bare_model_still_raises():
    with pytest.raises(LLMError, match="Cannot infer provider"):
        make_client("mistral-large")
