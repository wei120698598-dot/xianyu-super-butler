"""Runtime for the imported reply engine. Disabled accounts never reach it."""

import threading
import time

from loguru import logger

from app.new_ai_reply.agent import XianyuReplyBot
from app.new_ai_reply.gate import HumanReplyGate
from app.new_ai_reply.llm import LLMConfig
from app.new_ai_reply.store import data_dir, load_settings, save_settings
from app.new_ai_reply.style import ManualStyleLearner


class NewAIEngine:
    def __init__(self):
        self._lock = threading.RLock()
        self._bot = None
        self._bot_key = None
        self._gates = {}
        self._chats = {}
        self._manual_until = {}
        self._style_learner = None

    def is_enabled(self, cookie_id):
        try:
            return str(cookie_id) in load_settings()["enabled_accounts"]
        except Exception:
            return False

    def set_enabled(self, cookie_id, enabled):
        settings = load_settings()
        accounts = [item for item in settings["enabled_accounts"] if item != str(cookie_id)]
        if enabled:
            accounts.append(str(cookie_id))
        save_settings({"enabled_accounts": accounts})

    def _style_dir(self):
        return data_dir()

    def learner(self):
        if self._style_learner is None:
            self._style_learner = ManualStyleLearner(self._style_dir())
        return self._style_learner

    def gate_for(self, cookie_id):
        settings = load_settings()
        gate = self._gates.get(cookie_id)
        wait = int(settings["human_wait_seconds"])
        if gate is None:
            gate = HumanReplyGate(wait)
            self._gates[cookie_id] = gate
        else:
            gate.wait_seconds = wait
        return gate

    def _chat(self, cookie_id, chat_id):
        key = (str(cookie_id), str(chat_id))
        state = self._chats.get(key)
        if state is None:
            state = {"messages": [], "bargain_count": 0}
            self._chats[key] = state
        return state

    def remember_buyer(self, cookie_id, chat_id, text):
        with self._lock:
            state = self._chat(cookie_id, chat_id)
            state["messages"].append({"role": "user", "content": text})
            state["messages"] = state["messages"][-40:]

    def remember_assistant(self, cookie_id, chat_id, text, intent):
        with self._lock:
            state = self._chat(cookie_id, chat_id)
            state["messages"].append({"role": "assistant", "content": text})
            if intent == "price":
                state["bargain_count"] += 1
                state["messages"].append({
                    "role": "system",
                    "content": f"议价次数：{state['bargain_count']}",
                })
            state["messages"] = state["messages"][-40:]

    def context_for(self, cookie_id, chat_id, current_message):
        with self._lock:
            messages = list(self._chat(cookie_id, chat_id)["messages"])
        if messages and messages[-1] == {"role": "user", "content": current_message}:
            messages = messages[:-1]
        return messages

    def is_manual_mode(self, cookie_id, chat_id):
        key = (str(cookie_id), str(chat_id))
        until = self._manual_until.get(key)
        if not until:
            return False
        if time.time() >= until:
            self._manual_until.pop(key, None)
            return False
        return True

    def note_seller(self, cookie_id, chat_id, text, timestamp_ms):
        gate = self.gate_for(cookie_id)
        if gate.is_bot_echo(chat_id, text, timestamp_ms):
            return "echo"
        settings = load_settings()
        stripped = (text or "").strip()
        keywords = settings.get("toggle_keywords") or ""
        if stripped and stripped in keywords:
            key = (str(cookie_id), str(chat_id))
            if key in self._manual_until:
                self._manual_until.pop(key, None)
                gate.resume_auto(chat_id)
                return "toggle-off"
            self._manual_until[key] = time.time() + int(settings["manual_mode_timeout"])
            gate.invalidate(chat_id)
            return "toggle-on"
        context = self.context_for(cookie_id, chat_id, "")
        self.remember_assistant(cookie_id, chat_id, text, None)
        try:
            result = self.learner().learn(text, context + [{"role": "assistant", "content": text}])
            logger.info(f"【{cookie_id}】新AI人工话术学习：{result}")
        except Exception:
            logger.warning(f"【{cookie_id}】新AI人工话术样本未保存")
        gate.manual_reply(chat_id, timestamp_ms)
        return "manual"

    def expect_echo(self, cookie_id, chat_id, text, timestamp_ms):
        return self.gate_for(cookie_id).expect_echo(chat_id, text, timestamp_ms)

    def mark_sending(self, cookie_id, chat_id):
        self.gate_for(cookie_id).mark_sending(chat_id)

    def schedule(self, cookie_id, chat_id, timestamp_ms, reply):
        return self.gate_for(cookie_id).schedule(chat_id, timestamp_ms, reply)

    def item_description(self, cookie_id, item_id):
        if not item_id:
            return None
        from app.db_manager import db_manager

        raw = db_manager.get_item_info(cookie_id, item_id) or {}
        title = str(raw.get("item_title") or "").strip()
        price = str(raw.get("item_price") or "").strip()
        detail = str(raw.get("item_detail") or "").strip()
        if not any((title, price, detail)):
            return None
        detail = detail[:6000]
        return f"当前商品的信息如下：标题：{title}；价格：{price}；介绍：{detail}"

    def _bot_for(self, settings):
        key = (
            settings["backend"], settings["prompt"], settings["codebuddy_bin"],
            settings["codebuddy_model"], settings["codebuddy_timeout"],
            settings["base_url"], settings["model_name"], bool(settings.get("api_key")),
        )
        if self._bot is None or self._bot_key != key:
            self._bot = XianyuReplyBot(settings["prompt"], LLMConfig(settings), self._style_dir())
            self._bot_key = key
        return self._bot

    def generate(self, message, item_desc, context):
        settings = load_settings()
        with self._lock:
            bot = self._bot_for(settings)
        reply, intent = bot.generate_reply_with_intent(message, item_desc, context)
        text = (reply or "").strip()
        if len(text) > 800:
            raise RuntimeError("回复过长，本次不发送")
        return text, intent

    def invalidate_bot(self):
        with self._lock:
            self._bot = None
            self._bot_key = None


new_ai_engine = NewAIEngine()
