from __future__ import annotations

import os
import random
import re
import shutil
import subprocess
import tempfile
import time
from abc import ABC, abstractmethod


class LLMError(RuntimeError):
    pass


class LLMClient(ABC):
    name: str
    model: str

    @abstractmethod
    def generate(self, system: str, user: str) -> str: ...


def _with_retries(fn, attempts: int = 6, base_delay: float = 1.5):
    """Retry transient provider failures (rate limits, timeouts, 5xx) with backoff."""
    last: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except LLMError:
            raise
        except Exception as e:  # provider SDKs raise their own hierarchies
            last = e
            msg = str(e)
            if "invalid_request_error" in msg and "rate_limit" not in msg:
                break  # a malformed request will not succeed on retry
            if i + 1 < attempts:
                # Honour "try again in Xs" hints from rate-limit errors.
                hint = re.search(r"try again in ([0-9.]+)s", msg)
                wait = float(hint.group(1)) + 0.5 if hint else base_delay * (2 ** i)
                time.sleep(min(wait + random.random(), 30))
    raise LLMError(f"LLM call failed after {attempts} attempts: {last}") from last


class AnthropicClient(LLMClient):
    name = "anthropic"

    def __init__(self, model: str = "claude-sonnet-5", api_key: str | None = None):
        try:
            import anthropic
        except ImportError as e:
            raise LLMError("anthropic package not installed") from e
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise LLMError("ANTHROPIC_API_KEY is not set")
        self._client = anthropic.Anthropic(api_key=key)
        self.model = model

    def generate(self, system: str, user: str) -> str:
        response = _with_retries(lambda: self._client.messages.create(
            model=self.model,
            max_tokens=4000,
            # Determinism is a feature: the same question should yield the same SQL.
            temperature=0,
            system=system,
            messages=[{"role": "user", "content": user}],
        ))
        parts: list[str] = []
        for block in response.content:
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
        return "".join(parts)


class OpenAIClient(LLMClient):
    name = "openai"

    def __init__(self, model: str = "gpt-4.1", api_key: str | None = None,
                 base_url: str | None = None):
        try:
            import openai
        except ImportError as e:
            raise LLMError("openai package not installed (pip install promptquery[openai])") from e
        key = api_key or os.environ.get("OPENAI_API_KEY")
        if not key:
            raise LLMError("OPENAI_API_KEY is not set")
        # OPENAI_BASE_URL lets any OpenAI-compatible endpoint (Azure, vLLM, Ollama) be used.
        self._client = openai.OpenAI(
            api_key=key, base_url=base_url or os.environ.get("OPENAI_BASE_URL") or None)
        self.model = model

    # Reasoning-class OpenAI models (GPT-5.x/6.x, o1, o3, o4) accept
    # `max_completion_tokens` and reject the legacy `max_tokens`/`temperature` parameters.
    _REASONING_PREFIXES = ("gpt-5", "gpt-6", "o1", "o3", "o4")

    def _kwargs(self, system: str, user: str, reasoning: bool) -> dict:
        kwargs: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if reasoning:
            # Reasoning models reject `temperature`/`seed`; they sample internally. The budget
            # covers hidden reasoning tokens as well as the visible answer.
            kwargs["max_completion_tokens"] = 16000
        else:
            kwargs["max_tokens"] = 4000
            # Determinism is a feature: same question -> same SQL. temperature=0
            # plus a fixed seed gives best-effort reproducibility on chat models.
            kwargs["temperature"] = 0
            kwargs["seed"] = 0
        return kwargs

    def generate(self, system: str, user: str) -> str:
        reasoning = self.model.startswith(self._REASONING_PREFIXES)

        def call():
            nonlocal reasoning
            try:
                return self._client.chat.completions.create(**self._kwargs(system, user, reasoning))
            except Exception as e:
                # Unknown model family: switch parameter style once if the API says so.
                if "Unsupported parameter" in str(e) or "unsupported_parameter" in str(e):
                    reasoning = not reasoning
                    return self._client.chat.completions.create(**self._kwargs(system, user, reasoning))
                raise

        response = _with_retries(call)
        return response.choices[0].message.content or ""


class ClaudeCLIClient(LLMClient):
    """Runs prompts through a locally installed, logged-in Claude Code CLI (`claude -p`).

    Useful where no API key is available (local development / evaluation). The call is
    isolated: no tools, no settings/CLAUDE.md, no MCP servers, no session persistence, and it
    runs from an empty temp directory, so only our system prompt and user message are seen.
    """

    name = "claude-cli"

    def __init__(self, model: str = "sonnet", timeout: float = 180.0):
        self._bin = shutil.which("claude")
        if not self._bin:
            raise LLMError("claude CLI not found on PATH")
        self.model = model
        self.timeout = timeout
        self._cwd = tempfile.mkdtemp(prefix="pq-claude-cli-")

    def _call(self, system: str, user: str) -> str:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", dir=self._cwd, delete=False) as f:
            f.write(system)
            sp_path = f.name
        try:
            proc = subprocess.run(
                [self._bin, "-p", "--model", self.model, "--system-prompt-file", sp_path,
                 "--tools", "", "--setting-sources", "", "--strict-mcp-config",
                 "--no-session-persistence", "--disable-slash-commands",
                 "--output-format", "text"],
                input=user, capture_output=True, text=True, timeout=self.timeout, cwd=self._cwd,
            )
        finally:
            os.unlink(sp_path)
        if proc.returncode != 0:
            raise RuntimeError(f"claude CLI exited {proc.returncode}: {proc.stderr.strip()[:300]}")
        return proc.stdout

    def generate(self, system: str, user: str) -> str:
        return _with_retries(lambda: self._call(system, user))


_SQL_BLOCK_RE = re.compile(r"```(?:sql)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


_JSON_OBJ_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)


def extract_json(text: str) -> dict:
    """Parse the first JSON object in an LLM response (fenced or bare)."""
    import json

    match = _JSON_OBJ_RE.search(text)
    candidate = match.group(1) if match else None
    if candidate is None:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("no JSON object in response")
        candidate = text[start:end + 1]
    obj = json.loads(candidate)
    if not isinstance(obj, dict):
        raise ValueError("response JSON is not an object")
    return obj


def extract_sql(text: str) -> str:
    match = _SQL_BLOCK_RE.search(text)
    if match:
        return match.group(1).strip().rstrip(";").strip()
    return text.strip().rstrip(";").strip()


def make_client(model_spec: str | None = None) -> LLMClient:
    if model_spec:
        provider, _, model = model_spec.partition("/")
        if not model:
            # No explicit provider — guess from model name
            if provider.startswith("claude"):
                return AnthropicClient(model=provider)
            if provider.startswith(("gpt", "o1", "o3", "o4")):
                return OpenAIClient(model=provider)
            raise LLMError(
                f"Cannot infer provider from model {provider!r}. "
                "Use 'anthropic/<model>' or 'openai/<model>'."
            )
        if provider == "anthropic":
            return AnthropicClient(model=model)
        if provider == "openai":
            return OpenAIClient(model=model)
        if provider == "claude-cli":
            return ClaudeCLIClient(model=model)
        raise LLMError(f"Unknown provider: {provider!r}")

    if os.environ.get("ANTHROPIC_API_KEY"):
        return AnthropicClient()
    if os.environ.get("OPENAI_API_KEY"):
        return OpenAIClient()
    if shutil.which("claude"):
        return ClaudeCLIClient()
    raise LLMError(
        "No LLM available. Set ANTHROPIC_API_KEY or OPENAI_API_KEY (or install the claude CLI)."
    )
