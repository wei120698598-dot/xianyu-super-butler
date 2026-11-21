"""Per-product memory: identity cards, item bindings and confirmed Q&A.

Cascade resolution for a product link:
  1. exact fingerprint (normalized title + detail) -> reuse memory, zero LLM call
  2. fingerprint miss -> one LLM call to match/derive an identity card
  3. no match -> create a new product memory

All writes are atomic and private (0600). Item ids only ever map to a stable
memory key, so multiple links of the same product share one memory.
"""

import hashlib
import json
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from loguru import logger

from app.new_ai_reply.trace import redact


_LOCK = threading.RLock()
_INDEX = "_index.json"
MAX_QA = 200
MAX_PROMPT_CHARS = 8000
FP_DETAIL_CHARS = 300

_MARKETING = ("包邮", "秒发", "现货", "全新", "正品", "特价", "优惠", "清仓", "促销", "活动价", "实名")
_FULLWIDTH = {code: code - 0xFEE0 for code in range(0xFF01, 0xFF5F)}
_FULLWIDTH[0x3000] = 0x20

_IDENTITY_KEYS = ("品类", "核心卖点", "版本型号", "支持平台", "定位")


def _private_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".memory-", dir=str(path.parent))
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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize(text: str) -> str:
    """Normalize a product field for a stable fingerprint."""
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"https?://\S+|/~[^\s]+", " ", text)
    text = text.translate(_FULLWIDTH)
    text = re.sub(r"¥\s?\d+(?:\.\d+)?|\d+(?:\.\d+)?\s?[元块]", " ", text)
    for word in _MARKETING:
        text = text.replace(word, " ")
    text = re.sub(r"\s+", "", text)
    return text.strip()


def fingerprint(title: str, detail: str) -> str:
    """Stable id for identical product copy; changes when the copy changes."""
    raw = _normalize(title) + "|" + _normalize(detail)[:FP_DETAIL_CHARS]
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _qa_clean(text: str) -> str:
    text = redact(text or "")
    text = re.sub(r"https?://\S+|/~[^\s]+", "[链接已隐藏]", text)
    text = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "[手机号已隐藏]", text)
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[邮箱已隐藏]", text)
    return text.strip()


def _qa_suitable(text: str) -> bool:
    """Keep confirmed product facts, drop rants / temporary promises / injections."""
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= 200:
        return False
    if not re.search(r"[一-鿿A-Za-z]", text):
        return False
    if re.search(r"ai回复的啥|什么玩意|傻逼|滚蛋|你\*+|找找自己原因", text, re.IGNORECASE):
        return False
    if re.search(r"今天|明天|后天|稍后|一会儿|稍等|明天给|拍下就|马上给|先给你改|给你改价", text):
        return False
    if re.search(r"(?i)system|prompt|忽略.*(?:规则|指令)|提示词|只输出|扮演|你现在是|修改.*规则", text):
        return False
    if "[已隐藏]" in text:
        return False
    return True


def _identity_text(identity: dict) -> str:
    parts = []
    for key in _IDENTITY_KEYS:
        value = identity.get(key)
        if isinstance(value, list):
            value = ",".join(str(item) for item in value)
        if value:
            parts.append(f"{key}={value}")
    return "；".join(parts) if parts else "（无画像）"


class ProductMemory:
    def __init__(self, directory: str | Path):
        self.root = Path(directory) / "memory"
        with _LOCK:
            self.root.mkdir(parents=True, exist_ok=True)

    # ---- index / storage helpers ----
    def _read_json(self, path: Path, default):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default
        return data if isinstance(data, type(default)) else default

    def _read_index(self) -> dict:
        data = self._read_json(self.root / _INDEX, {})
        if not isinstance(data.get("memories"), dict):
            data["memories"] = {}
        if not isinstance(data.get("bindings"), dict):
            data["bindings"] = {}
        return data

    def _write_index(self, index: dict) -> None:
        _private_write(self.root / _INDEX, json.dumps(index, ensure_ascii=False, indent=2) + "\n")

    def _memory_path(self, key: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_-]", "", key)
        return self.root / f"{safe}.json"

    def _read_memory(self, key: str) -> dict:
        data = self._read_json(self._memory_path(key), {})
        if not isinstance(data.get("qa"), list):
            data["qa"] = []
        return data

    def _write_memory(self, key: str, memory: dict) -> None:
        _private_write(self._memory_path(key), json.dumps(memory, ensure_ascii=False, indent=2) + "\n")

    # ---- model identity card ----
    def _identity_card(self, client, title: str, detail: str, candidates: dict) -> dict:
        """One LLM call: match an existing product or derive a fresh identity card."""
        catalog_lines = []
        for key, meta in candidates.items():
            catalog_lines.append(f"- {key}: {_identity_text(meta.get('identity', {}))}")
        catalog = "\n".join(catalog_lines) if catalog_lines else "（暂无已有商品）"
        system = (
            "你是闲鱼商品的记忆比对助手。判断【当前商品】是否与【已有商品】中的某一个是同一个商品"
            "（同一商品的多个链接、下架重上、文案微调都算同一个）。只输出 JSON，不要解释。"
            '\n已有商品字段：品类、核心卖点、版本型号、支持平台（数组）、定位。'
            '\n输出格式：{"match":"匹配到的key，没有则空字符串","identity":{'
            '"品类":"","核心卖点":"","版本型号":"","支持平台":[],"定位":""}}'
        )
        prompt = (
            f"【已有商品】\n{catalog}\n\n"
            f"【当前商品标题】{title}\n"
            f"【当前商品详情】{detail[:1500]}"
        )
        raw = client.complete(
            [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            temperature=0.0,
        )
        text = (raw or "").strip()
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
        payload = json.loads(text)
        if not isinstance(payload, dict):
            raise ValueError("identity payload is not an object")
        identity = payload.get("identity")
        if not isinstance(identity, dict):
            identity = {}
        clean = {}
        for key in _IDENTITY_KEYS:
            value = identity.get(key)
            if isinstance(value, list):
                clean[key] = [str(item)[:40] for item in value[:10]]
            elif value:
                clean[key] = str(value)[:80]
        match = payload.get("match")
        return {"match": match if isinstance(match, str) else "", "identity": clean}

    @staticmethod
    def _fallback_identity(title: str) -> dict:
        return {"品类": (title or "")[:40] or "未识别商品"}

    # ---- cascade resolution ----
    def resolve(self, item_id: str, title: str, detail: str, client=None) -> str:
        """Return the memory key for this item id, applying the 3-step cascade."""
        if not item_id:
            return ""
        item_id = str(item_id)
        with _LOCK:
            index = self._read_index()
            memories = index["memories"]

            # 1) binding cache
            key = index["bindings"].get(item_id)
            if key and key in memories:
                return key

            fp = fingerprint(title, detail)

            # 2) exact fingerprint
            for existing, meta in memories.items():
                if meta.get("fingerprint") == fp:
                    index["bindings"][item_id] = existing
                    self._write_index(index)
                    logger.info(f"商品记忆：指纹命中，{item_id} -> {existing}")
                    return existing

            # 3) model identity match (only on fingerprint miss)
            identity = None
            matched = ""
            if client is not None and memories:
                try:
                    result = self._identity_card(client, title, detail, memories)
                    matched, identity = result["match"], result["identity"]
                except Exception as exc:
                    logger.warning(f"商品记忆：身份卡调用失败，转新建：{type(exc).__name__}")

            if matched and matched in memories:
                index["bindings"][item_id] = matched
                meta = memories[matched]
                meta.setdefault("fingerprints", [])
                if fp not in meta["fingerprints"]:
                    meta["fingerprints"].append(fp)
                meta["updated_at"] = _now()
                self._write_index(index)
                logger.info(f"商品记忆：身份卡命中，{item_id} -> {matched}")
                return matched

            # 4) create new
            key = "m_" + fp
            if key in memories:  # unlikely: fp collision guard
                key = "m_" + hashlib.sha1((item_id + _now()).encode()).hexdigest()[:12]
            if identity is None or not any(identity.get(k) for k in _IDENTITY_KEYS):
                identity = self._fallback_identity(title)
            memories[key] = {
                "fingerprint": fp,
                "fingerprints": [fp],
                "identity": identity,
                "item_ids": [item_id],
                "created_at": _now(),
                "updated_at": _now(),
            }
            index["bindings"][item_id] = key
            self._write_index(index)
            self._write_memory(key, {"key": key, "qa": [], "created_at": _now(), "updated_at": _now()})
            logger.info(f"商品记忆：新建 {key}（{item_id}，{_identity_text(identity)}）")
            return key

    # ---- learning ----
    def learn(self, key: str, question: str, answer: str) -> str:
        """Summarize a confirmed human Q&A into this product's memory."""
        if not key:
            return "skipped"
        if not _qa_suitable(question) or not _qa_suitable(answer):
            return "skipped"
        q, a = _qa_clean(question), _qa_clean(answer)
        if not q or not a:
            return "skipped"
        with _LOCK:
            if not (self.root / _INDEX).exists():
                return "skipped"
            memory = self._read_memory(key)
            qa = [row for row in memory["qa"] if isinstance(row, dict) and
                  all(isinstance(row.get(f), str) for f in ("q", "a"))]
            target = next((row for row in qa if row["q"] == q), None)
            if target:
                target["a"] = a
                target["hits"] = int(target.get("hits", 1)) + 1
                target["time"] = _now()
            else:
                qa.append({"q": q, "a": a, "hits": 1, "time": _now()})
            memory["qa"] = qa[-MAX_QA:]
            memory["updated_at"] = _now()
            self._write_memory(key, memory)
        return "learned"

    # ---- injection ----
    def load_prompt(self, key: str) -> str:
        """Render this product's memory for the reply prompt."""
        if not key:
            return ""
        with _LOCK:
            index = self._read_index()
            meta = index["memories"].get(key)
            memory = self._read_memory(key)
        if not meta:
            return ""
        qa = sorted(
            (row for row in memory["qa"] if isinstance(row, dict)),
            key=lambda row: (int(row.get("hits", 1)), row.get("time", "")),
            reverse=True,
        )[:30]
        lines = [
            "【本商品专属记忆（仅适用于当前商品，不得引用其它商品的信息）】",
            "商品画像：" + _identity_text(meta.get("identity", {})),
            "卖家本人已确认的本商品问答（作为口径参考，不要照抄；记忆未覆盖的问题按商品详情与规则回答）：",
        ]
        for row in qa:
            lines.append(f"- 问：{row['q']} → 答：{row['a']}（确认{int(row.get('hits', 1))}次）")
        text = "\n".join(lines)
        return text if len(text) <= MAX_PROMPT_CHARS else text[:MAX_PROMPT_CHARS]
