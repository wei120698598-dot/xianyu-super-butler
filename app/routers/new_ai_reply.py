"""Settings and dry-run test for the imported reply engine."""

from __future__ import annotations

import asyncio
import shutil
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.new_ai_reply.engine import new_ai_engine
from app.new_ai_reply.llm import model_failure_reason
from app.new_ai_reply.store import load_settings, public_settings, save_settings, validate_settings


class NewAISettingsUpdate(BaseModel):
    cookie_id: str = Field(min_length=1, max_length=64)
    enabled: bool = False
    backend: str
    codebuddy_bin: str
    codebuddy_model: str = ""
    codebuddy_timeout: int = 90
    api_key: str = ""
    base_url: str = ""
    model_name: str = ""
    human_wait_seconds: int = 180
    manual_mode_timeout: int = 3600
    toggle_keywords: str = ""
    prompt: str


class NewAITestRequest(BaseModel):
    cookie_id: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=1000)
    item_title: str = "测试商品"
    item_price: str = ""
    item_desc: str = "仅用于测试，不会发送到闲鱼。"


def create_new_ai_reply_router(
    get_current_user: Callable[..., dict[str, Any]],
    db_manager: Any,
) -> APIRouter:
    router = APIRouter()

    def require_account(cookie_id: str, current_user: dict[str, Any]) -> None:
        user_cookies = db_manager.get_all_cookies(current_user["user_id"])
        if cookie_id not in user_cookies:
            raise HTTPException(status_code=403, detail="无权限操作该账号")

    @router.get("/new-ai-reply/settings/{cookie_id}")
    def get_settings(cookie_id: str, current_user: dict[str, Any] = Depends(get_current_user)):
        require_account(cookie_id, current_user)
        settings = load_settings()
        payload = public_settings(settings, cookie_id)
        payload["codebuddy_available"] = bool(shutil.which(settings["codebuddy_bin"]))
        return payload

    @router.put("/new-ai-reply/settings")
    def update_settings(update: NewAISettingsUpdate, current_user: dict[str, Any] = Depends(get_current_user)):
        require_account(update.cookie_id, current_user)
        try:
            cleaned = validate_settings(update.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        current = load_settings()
        if not cleaned["api_key"]:
            cleaned["api_key"] = current.get("api_key") or ""
        save_settings(cleaned)
        new_ai_engine.set_enabled(update.cookie_id, update.enabled)
        new_ai_engine.invalidate_bot()
        return {"message": "新AI自动回复配置已保存"}

    @router.post("/new-ai-reply/test")
    async def test_reply(test: NewAITestRequest, current_user: dict[str, Any] = Depends(get_current_user)):
        require_account(test.cookie_id, current_user)
        description = (
            f"当前商品的信息如下：标题：{test.item_title[:120]}；"
            f"价格：{test.item_price[:40]}；介绍：{test.item_desc[:2000]}"
        )
        try:
            reply, intent = await asyncio.to_thread(
                new_ai_engine.generate, test.message, description, []
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=model_failure_reason(exc)) from exc
        sent = bool(reply) and reply != "-"
        return {
            "message": "测试完成，未发送到闲鱼",
            "reply": reply if sent else "",
            "intent": intent or "",
            "would_send": sent,
        }

    return router
