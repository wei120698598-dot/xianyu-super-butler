"""Text-only model backends. CodeBuddy never receives Xianyu credentials."""

import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from app.new_ai_reply.process_utils import run_isolated
from app.new_ai_reply.trace import traced_call


class ModelCallError(RuntimeError):
    """An allowlisted diagnostic reason, never raw provider output."""
    REASONS = {
        "rate_limit": "模型额度或频率限制",
        "timeout": "模型调用超时",
        "cli_failed": "模型CLI调用失败，请检查登录状态或额度",
        "invalid_response": "模型返回无效或空的JSON",
    }

    def __init__(self, reason):
        self.reason = reason if reason in self.REASONS else "cli_failed"
        super().__init__(self.REASONS[self.reason])


def model_failure_reason(exc):
    if isinstance(exc, ModelCallError):
        return ModelCallError.REASONS[exc.reason]
    if isinstance(exc, RuntimeError):
        text = str(exc).strip()
        allowed = ("找不到 CodeBuddy", "未配置", "无法启动", "CodeBuddy", "回复过长", "LLM 后端")
        if text.startswith(allowed):
            return text[:160]
    if getattr(exc, "status_code", None) == 429:
        return "模型额度或频率限制"
    if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)):
        return "模型调用超时"
    return f"模型调用失败 {type(exc).__name__}"


class LLMConfig:
    def __init__(self, settings):
        self.backend = settings["backend"]
        self.api_key = settings.get("api_key") or ""
        self.base_url = settings.get("base_url") or "https://dashscope.aliyuncs.com/compatible-mode/v1"
        self.model_name = settings.get("model_name") or "qwen-plus"
        self.codebuddy_bin = settings.get("codebuddy_bin") or "codebuddy"
        self.codebuddy_model = (settings.get("codebuddy_model") or "").strip()
        self.codebuddy_timeout = float(settings.get("codebuddy_timeout") or 90)


def create_llm_client(config):
    if config.backend == "codebuddy":
        return CodeBuddyClient(config)
    if config.backend == "openai":
        return OpenAIClient(config)
    raise ValueError("LLM 后端必须是 openai 或 codebuddy")


class OpenAIClient:
    def __init__(self, config):
        from openai import OpenAI

        if not config.api_key:
            raise RuntimeError("未配置 OpenAI 兼容接口密钥")
        self.model_name = config.model_name
        self.base_url = config.base_url
        self.client = OpenAI(api_key=config.api_key, base_url=config.base_url)

    def complete(self, messages, temperature=0.4, enable_search=False):
        options = dict(
            model=self.model_name,
            messages=messages,
            temperature=temperature,
            max_tokens=500,
            top_p=0.8,
        )
        if enable_search:
            options["extra_body"] = {"enable_search": True}
        with traced_call("openai", endpoint=self.base_url, **options) as trace:
            response = self.client.chat.completions.create(**options)
            trace.response(response)
            return response.choices[0].message.content


class CodeBuddyClient:
    # Deliberately do not inherit arbitrary environment variables (cookies,
    # model keys, NODE_OPTIONS, or CLI permission overrides) from the seller app.
    ENV_KEYS = {
        "HOME", "USERPROFILE", "PATH", "TMPDIR", "TEMP", "TMP",
        "LANG", "LC_ALL", "LC_CTYPE", "SYSTEMROOT",
        "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS",
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
        "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    }

    def __init__(self, config):
        executable = shutil.which(config.codebuddy_bin)
        if not executable:
            raise RuntimeError("找不到 CodeBuddy CLI，请先安装并登录，或填写可执行文件路径")
        self.executable = str(Path(executable).absolute())
        self.timeout = config.codebuddy_timeout
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("CodeBuddy 超时必须是正数秒数")
        self.model = config.codebuddy_model

    def complete(self, messages, temperature=0.4, enable_search=False):
        with traced_call(
            "codebuddy", model=self.model or "CLI default", messages=messages,
            parameters={"output_format": "json", "tools": [], "strict_mcp_config": True,
                        "mcp_servers": {}, "setting_sources": [], "session_persistence": False,
                        "timeout_seconds": self.timeout},
            unsupported_options={"temperature": temperature, "enable_search": enable_search},
        ) as trace:
            return self._complete(messages, trace)

    def _complete(self, messages, trace):
        system_prompt = "\n\n".join(
            message["content"] for message in messages if message["role"] == "system"
        )
        conversation = [message for message in messages if message["role"] != "system"]
        if not conversation:
            raise ValueError("CodeBuddy 请求缺少用户消息")
        prompt = json.dumps(conversation, ensure_ascii=False)
        env = {key: value for key, value in os.environ.items() if key in self.ENV_KEYS}

        with tempfile.TemporaryDirectory(prefix="xianyu-codebuddy-") as workdir:
            system_file = Path(workdir) / "system.txt"
            system_file.write_text(system_prompt, encoding="utf-8")
            command = [
                self.executable, "--print", "--output-format", "json",
                "--effort", "low",
                "--tools", "", "--strict-mcp-config",
                "--mcp-config", '{"mcpServers":{}}',
                "--setting-sources", "", "--no-session-persistence",
                "--system-prompt-file", str(system_file),
            ]
            if self.model:
                command.extend(["--model", self.model])
            try:
                result = run_isolated(
                    command, input=prompt, text=True, encoding="utf-8",
                    capture_output=True, timeout=self.timeout, cwd=workdir,
                    env=env, check=False,
                )
            except subprocess.TimeoutExpired:
                raise ModelCallError("timeout") from None
            except OSError:
                raise RuntimeError("无法启动 CodeBuddy，请检查安装和可执行文件路径") from None

        if result.returncode != 0 or not result.stdout.strip():
            error = result.stderr or ""
            limited = any(marker in error.lower() for marker in
                          ("429", "rate limit", "too many requests", "quota", "额度", "频率限制"))
            raise ModelCallError("rate_limit" if limited else "cli_failed")
        text = self._parse_result(result.stdout)
        trace.response(json.loads(result.stdout))
        return text

    @staticmethod
    def _parse_result(output):
        try:
            payload = json.loads(output)
        except (ValueError, TypeError):
            raise RuntimeError("CodeBuddy 返回了无效 JSON，本次不发送回复") from None
        events = payload if isinstance(payload, list) else [payload]
        results = [event for event in events if isinstance(event, dict) and event.get("type") == "result"]
        if not results:
            raise RuntimeError("CodeBuddy 缺少最终结果，本次不发送回复")
        final = results[-1]
        if final.get("is_error") or final.get("subtype") != "success" or final.get("permission_denials"):
            raise RuntimeError("CodeBuddy 未成功完成请求，本次不发送回复")
        text = final.get("result")
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError("CodeBuddy 返回空回复，本次不发送回复")
        return text.strip()
