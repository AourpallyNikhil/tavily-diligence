"""Thin OpenAI-compatible chat helper with tracing and JSON coercion.

Kept deliberately small. An agent framework would add abstraction without adding
capability here: the control flow of this system is a fixed pipeline, not an
autonomous loop, and that is the point -- a diligence report that takes a
different path on every run is not auditable.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from openai import OpenAI

from .tracing import INPUT_VALUE, OUTPUT_VALUE, record_llm, span

_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: int = 0
    by_model: dict[str, dict[str, int]] = field(default_factory=dict)

    def add(self, model: str, usage: Any) -> None:
        p = getattr(usage, "prompt_tokens", 0) or 0
        c = getattr(usage, "completion_tokens", 0) or 0
        self.prompt_tokens += p
        self.completion_tokens += c
        self.calls += 1
        slot = self.by_model.setdefault(model, {"prompt": 0, "completion": 0, "calls": 0})
        slot["prompt"] += p
        slot["completion"] += c
        slot["calls"] += 1


def extract_json(text: str) -> Any:
    """Models wrap JSON in prose or fences often enough that this is load-bearing."""
    text = (text or "").strip()
    if not text:
        raise ValueError("empty model response")

    fenced = _FENCE.search(text)
    if fenced:
        text = fenced.group(1).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Fall back to the outermost balanced object or array.
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError(f"could not parse JSON from model response: {text[:200]!r}")


class LLM:
    def __init__(self, api_key: str, base_url: str, usage: Usage | None = None) -> None:
        self._client = OpenAI(api_key=api_key, base_url=base_url)
        self.usage = usage or Usage()

    def chat_json(
        self,
        *,
        model: str,
        system: str,
        user: str,
        span_name: str,
        temperature: float = 0.0,
        max_tokens: int = 2000,
        attributes: dict[str, Any] | None = None,
    ) -> Any:
        with span(span_name, attributes) as sp:
            sp.set_attribute(INPUT_VALUE, user[:4000])
            resp = self._client.chat.completions.create(
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
            content = resp.choices[0].message.content or ""
            sp.set_attribute(OUTPUT_VALUE, content[:4000])
            record_llm(sp, model=model, usage=resp.usage)
            self.usage.add(model, resp.usage)
            return extract_json(content)
