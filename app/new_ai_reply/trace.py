"""Opt-in, per-test full AI traces. Never log authentication material."""

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import os
import re
import time
import uuid


_EVENTS = ContextVar("ai_trace_events", default=None)
_SINK = ContextVar("ai_trace_sink", default=None)
_STAGE = ContextVar("ai_trace_stage", default="reply")
_SECRET_KEY = re.compile(r"(?i)^(?:.*(?:password|secret|cookie|authorization|credential).*|.*(?:api[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token)|token|signature|sign|sid|unb)$")


def redact(value):
    """Preserve full content, replacing credentials rather than truncating it."""
    if isinstance(value, dict):
        return {str(k): "[已隐藏]" if _SECRET_KEY.match(str(k)) else redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if not isinstance(value, str):
        if callable(getattr(type(value), "model_dump", None)):
            return redact(value.model_dump(mode="json"))
        if hasattr(value, "__dict__"):
            return redact(vars(value))
        return f"[{type(value).__name__}]"
    hidden = set()
    for key, secret in os.environ.items():
        if _SECRET_KEY.match(key) and secret:
            hidden.add(secret)
            if "COOKIE" in key.upper():
                for part in secret.split(";"):
                    if "=" in part:
                        fragment = part.split("=", 1)[1].strip()
                        if len(fragment) >= 3:
                            hidden.add(fragment)
    for secret in sorted(hidden, key=len, reverse=True):
        value = value.replace(secret, "[已隐藏]")
    value = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [已隐藏]", value)
    value = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[密钥已隐藏]", value)
    value = re.sub(r"(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|密码|提取码|验证码|激活码)\s*[\"']?\s*[:=：]\s*[\"']?)[^\s\"',;}]+", r"\1[已隐藏]", value)
    value = re.sub(r"(?im)^\s*(cookie|authorization)\s*[:=].*$", r"\1: [已隐藏]", value)
    value = re.sub(r"(?i)(https?://)[^/\s:@]+:[^/\s@]+@", r"\1[已隐藏]@", value)
    value = re.sub(r"(?i)([?&](?:token|access_token|api_key|key|signature|sign|sid|_m_h5_tk)=)[^&#\s]+", r"\1[已隐藏]", value)
    value = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)
    return value


@contextmanager
def capture_trace(sink=None):
    events = []
    token = _EVENTS.set(events)
    sink_token = _SINK.set(sink)
    try:
        yield events
    finally:
        _SINK.reset(sink_token)
        _EVENTS.reset(token)


@contextmanager
def trace_stage(stage):
    token = _STAGE.set(stage)
    try:
        yield
    finally:
        _STAGE.reset(token)


def _record(event):
    events = _EVENTS.get()
    if events is not None:
        payload = redact({"time": datetime.now(timezone.utc).isoformat(), **event})
        events.append(payload)
        sink = _SINK.get()
        if sink is not None:
            try:
                sink(payload)
            except Exception:
                pass  # Diagnostic output must not change the generated reply.



class _Call:
    def __init__(self, backend, request):
        self.enabled = _EVENTS.get() is not None
        self.id = uuid.uuid4().hex[:12]
        self.started = time.monotonic()
        self.stage = _STAGE.get()
        if self.enabled:
            _record({"event": "ai_request", "request_id": self.id,
                     "stage": self.stage, "backend": backend, "request": request})

    def response(self, response):
        if self.enabled:
            _record({"event": "ai_response", "request_id": self.id,
                     "stage": self.stage, "duration_ms": round((time.monotonic() - self.started) * 1000),
                     "response": response})


@contextmanager
def traced_call(backend, **request):
    call = _Call(backend, request)
    try:
        yield call
    except Exception as exc:
        if call.enabled:
            status = getattr(exc, "status_code", None)
            _record({"event": "ai_error", "request_id": call.id,
                     "stage": call.stage,
                     "duration_ms": round((time.monotonic() - call.started) * 1000),
                     "error_type": type(exc).__name__,
                     "status_code": status if isinstance(status, int) else None})
        # Provider exception messages may contain credentials and account data.
        raise
