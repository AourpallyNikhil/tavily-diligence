"""OpenTelemetry instrumentation.

Plain OTel with OpenInference semantic conventions, exported over OTLP/HTTP. The
default collector is a locally-run Arize Phoenix, but nothing here is
Phoenix-specific: the same spans export unchanged to Langfuse, LangSmith, Jaeger,
or any OTLP endpoint by setting OTEL_EXPORTER_OTLP_ENDPOINT. Choosing the open
standard over a vendor SDK is the whole point -- instrument once, choose the
backend later.

Tracing here is not decoration. The eval harness reads these spans back to build
its claim-level precision dataset (see eval/run_eval.py), so the instrumentation
and the evaluation are one system rather than two.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_INITIALISED = False
_TRACER: trace.Tracer | None = None

# OpenInference semantic conventions. Imported defensively: the package is small
# and occasionally lags OTel releases, and a tracing dependency should never be
# what stops a diligence report from being produced.
try:
    from openinference.semconv.trace import SpanAttributes

    SPAN_KIND = SpanAttributes.OPENINFERENCE_SPAN_KIND
    LLM_MODEL = SpanAttributes.LLM_MODEL_NAME
    LLM_PROMPT_TOKENS = SpanAttributes.LLM_TOKEN_COUNT_PROMPT
    LLM_COMPLETION_TOKENS = SpanAttributes.LLM_TOKEN_COUNT_COMPLETION
    INPUT_VALUE = SpanAttributes.INPUT_VALUE
    OUTPUT_VALUE = SpanAttributes.OUTPUT_VALUE
except Exception:  # pragma: no cover - fallback to literal convention keys
    SPAN_KIND = "openinference.span.kind"
    LLM_MODEL = "llm.model_name"
    LLM_PROMPT_TOKENS = "llm.token_count.prompt"
    LLM_COMPLETION_TOKENS = "llm.token_count.completion"
    INPUT_VALUE = "input.value"
    OUTPUT_VALUE = "output.value"


def init_tracing(service_name: str = "tavily-diligence") -> None:
    """Idempotent. Silently degrades to no-op export if no collector is reachable."""
    global _INITIALISED, _TRACER
    if _INITIALISED:
        return

    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))

    endpoint = os.getenv(
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:6006/v1/traces"),
    )
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    except Exception:
        # No collector configured or exporter unavailable: spans are still created
        # (so the code paths are identical) but go nowhere.
        pass

    trace.set_tracer_provider(provider)
    _TRACER = trace.get_tracer(__name__)
    _INITIALISED = True


def tracer() -> trace.Tracer:
    if _TRACER is None:
        init_tracing()
    assert _TRACER is not None
    return _TRACER


@contextmanager
def span(name: str, attributes: dict[str, Any] | None = None) -> Iterator[trace.Span]:
    with tracer().start_as_current_span(name) as sp:
        for k, v in (attributes or {}).items():
            if v is not None:
                sp.set_attribute(k, v)
        yield sp


def record_llm(sp: trace.Span, *, model: str, usage: Any, kind: str = "LLM") -> None:
    """Attach OpenInference LLM attributes so token/cost analysis works in any backend."""
    sp.set_attribute(SPAN_KIND, kind)
    sp.set_attribute(LLM_MODEL, model)
    if usage is not None:
        prompt = getattr(usage, "prompt_tokens", None)
        completion = getattr(usage, "completion_tokens", None)
        if prompt is not None:
            sp.set_attribute(LLM_PROMPT_TOKENS, prompt)
        if completion is not None:
            sp.set_attribute(LLM_COMPLETION_TOKENS, completion)
