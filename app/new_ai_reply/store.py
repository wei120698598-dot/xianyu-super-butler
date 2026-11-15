"""Local settings for the imported reply engine. Secrets stay out of API responses."""

import json
import os
import tempfile
import threading
from pathlib import Path

from app.new_ai_reply.agent import parse_reply_prompt


_LOCK = threading.RLock()
_DIR = Path(__file__).resolve().parents[2] / "data" / "new_ai_reply"
_FILE = _DIR / "settings.json"
_TEMPLATE = Path(__file__).with_name("prompt_template.txt")
_OLD_ENV = Path("/Users/maoyan/orca/projects/xianyu-auto-reply/XianyuAutoAgent/.env")
_MAX_PROMPT = 100_000


def _read_old_env(name, fallback):
    try:
        for line in _OLD_ENV.read_text(encoding="utf-8").splitlines():
            if not line.startswith(f"{name}="):
                continue
            value = line.split("=", 1)[1].strip().strip("'\"")
            if value and len(value) <= 80 and "\n" not in value:
                return value
    except OSError:
        pass
    return fallback


def default_settings():
    prompt = ""
    try:
        prompt = _TEMPLATE.read_text(encoding="utf-8")
    except OSError:
        prompt = ""
    return {
        "enabled_accounts": [],
        "backend": "codebuddy",
        "codebuddy_bin": _read_old_env("CODEBUDDY_BIN", "codebuddy"),
        "codebuddy_model": _read_old_env("CODEBUDDY_MODEL", "hy3"),
        "codebuddy_timeout": 90,
        "api_key": "",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model_name": "qwen-plus",
        "human_wait_seconds": 180,
        "manual_mode_timeout": 3600,
        "toggle_keywords": _read_old_env("TOGGLE_KEYWORDS", "。"),
        "prompt": prompt,
    }


def _private_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".settings-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_settings():
    with _LOCK:
        if not _FILE.exists():
            settings = default_settings()
            _private_write(_FILE, json.dumps(settings, ensure_ascii=False, indent=2) + "\n")
            return settings
        try:
            raw = json.loads(_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default_settings()
        if not isinstance(raw, dict):
            return default_settings()
        settings = default_settings()
        settings.update({key: raw.get(key, settings[key]) for key in settings})
        if not isinstance(settings["enabled_accounts"], list):
            settings["enabled_accounts"] = []
        settings["enabled_accounts"] = [str(item) for item in settings["enabled_accounts"] if str(item).strip()]
        return settings


def save_settings(settings):
    with _LOCK:
        current = load_settings()
        current.update(settings)
        _private_write(_FILE, json.dumps(current, ensure_ascii=False, indent=2) + "\n")
        return current


def data_dir():
    _DIR.mkdir(parents=True, exist_ok=True)
    return _DIR


def public_settings(settings, cookie_id=None):
    return {
        "enabled": bool(cookie_id and cookie_id in settings["enabled_accounts"]),
        "backend": settings["backend"],
        "codebuddy_bin": settings["codebuddy_bin"],
        "codebuddy_model": settings["codebuddy_model"],
        "codebuddy_timeout": settings["codebuddy_timeout"],
        "api_key_configured": bool(settings.get("api_key")),
        "base_url": settings["base_url"],
        "model_name": settings["model_name"],
        "human_wait_seconds": settings["human_wait_seconds"],
        "manual_mode_timeout": settings["manual_mode_timeout"],
        "toggle_keywords": settings["toggle_keywords"],
        "prompt": settings["prompt"],
    }


def validate_settings(payload):
    backend = str(payload.get("backend") or "").strip().lower()
    if backend not in {"codebuddy", "openai"}:
        raise ValueError("模型后端只能是 codebuddy 或 openai")
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or len(prompt) > _MAX_PROMPT:
        raise ValueError("统一提示词过长或不是文本")
    parse_reply_prompt(prompt)
    timeout = payload.get("codebuddy_timeout")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 5 <= float(timeout) <= 180:
        raise ValueError("CodeBuddy 超时需在 5 到 180 秒之间")
    wait = payload.get("human_wait_seconds")
    if isinstance(wait, bool) or not isinstance(wait, int) or not 1 <= wait <= 604800:
        raise ValueError("人工回复等待需在 1 秒到 7 天之间")
    manual = payload.get("manual_mode_timeout")
    if isinstance(manual, bool) or not isinstance(manual, int) or not 60 <= manual <= 604800:
        raise ValueError("人工接管超时需在 1 分钟到 7 天之间")
    binary = str(payload.get("codebuddy_bin") or "").strip()
    if not binary or len(binary) > 300 or any(char.isspace() for char in binary):
        raise ValueError("CodeBuddy 路径不能为空，也不能包含空格")
    toggle = payload.get("toggle_keywords")
    if not isinstance(toggle, str) or len(toggle) > 80:
        raise ValueError("人工接管词过长")
    model = str(payload.get("codebuddy_model") or "").strip()
    if len(model) > 80:
        raise ValueError("CodeBuddy 模型名过长")
    base_url = str(payload.get("base_url") or "").strip()
    if base_url and not base_url.startswith(("http://", "https://")):
        raise ValueError("接口地址必须以 http:// 或 https:// 开头")
    if len(base_url) > 300:
        raise ValueError("接口地址过长")
    model_name = str(payload.get("model_name") or "").strip()
    if len(model_name) > 80:
        raise ValueError("模型名称过长")
    api_key = payload.get("api_key") or ""
    if not isinstance(api_key, str) or len(api_key) > 500:
        raise ValueError("接口密钥无效")
    return {
        "backend": backend,
        "prompt": prompt,
        "codebuddy_timeout": int(timeout),
        "human_wait_seconds": wait,
        "manual_mode_timeout": manual,
        "codebuddy_bin": binary,
        "codebuddy_model": model,
        "toggle_keywords": toggle,
        "base_url": base_url or "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model_name": model_name or "qwen-plus",
        "api_key": api_key,
    }
