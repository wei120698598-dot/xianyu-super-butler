"""Per-conversation human-priority timers; no network or model access."""

import asyncio
import time
from collections import OrderedDict

from loguru import logger


class HumanReplyGate:
    def __init__(self, wait_seconds=180):
        if isinstance(wait_seconds, bool) or not 1 <= wait_seconds <= 604800:
            raise ValueError("HUMAN_REPLY_WAIT_SECONDS must be between 1 and 604800")
        self.wait_seconds = wait_seconds
        self.human_chats = set()
        self.last_manual_ms = {}
        self.last_buyer_ms = {}
        self.versions = {}
        self.pending = {}
        self.jobs = {}
        self.suspended = False
        self.tasks = set()
        self.seen = OrderedDict()
        self.outbound = []
        self.bot_history = []

    def first_delivery(self, key):
        if key in self.seen:
            return False
        self.seen[key] = None
        if len(self.seen) > 10000:
            self.seen.popitem(last=False)
        return True

    def invalidate(self, chat_id):
        self.versions[chat_id] = self.versions.get(chat_id, 0) + 1
        self.jobs.pop(chat_id, None)
        task = self.pending.pop(chat_id, None)
        if task is not None:
            task.cancel()

    def manual_reply(self, chat_id, timestamp_ms):
        self.human_chats.add(chat_id)
        self.last_manual_ms[chat_id] = max(timestamp_ms, self.last_manual_ms.get(chat_id, 0))
        self.invalidate(chat_id)

    def resume_auto(self, chat_id):
        self.human_chats.discard(chat_id)
        self.invalidate(chat_id)

    def expect_echo(self, chat_id, text, timestamp_ms):
        token = (chat_id, text, timestamp_ms)
        self.outbound.append(token)
        self.outbound = self.outbound[-200:]
        self.bot_history.append(token)
        self.bot_history = self.bot_history[-500:]
        return token

    def forget_echo(self, token):
        if token in self.outbound:
            self.outbound.remove(token)
        if token in self.bot_history:
            self.bot_history.remove(token)

    def is_recent_bot_text(self, chat_id, text, timestamp_ms):
        return any(cid == chat_id and sent_text == text and -5000 <= timestamp_ms - sent_ms <= 600000
                   for cid, sent_text, sent_ms in self.bot_history)

    def is_bot_echo(self, chat_id, text, timestamp_ms):
        # The sync reminder exposes text/time, not a verified outbound UUID.
        # Keep a short one-use match window; do not classify every self message
        # as human or suppress a genuine manual reply indefinitely.
        for token in list(self.outbound):
            cid, sent_text, sent_ms = token
            if timestamp_ms - sent_ms > 120000:
                self.outbound.remove(token)
                continue
            if cid == chat_id and sent_text == text and -5000 <= timestamp_ms - sent_ms <= 120000:
                self.outbound.remove(token)
                return True
        return False

    def schedule(self, chat_id, timestamp_ms, reply):
        tag = f"会话 {chat_id} 消息 {timestamp_ms}"
        if timestamp_ms <= self.last_manual_ms.get(chat_id, -1):
            logger.info(f"{tag} 不回复：消息早于最近人工回复")
            return None
        if timestamp_ms < self.last_buyer_ms.get(chat_id, -1):
            logger.info(f"{tag} 不回复：已有更新的买家消息")
            return None
        replaced = chat_id in self.jobs
        self.last_buyer_ms[chat_id] = timestamp_ms
        self.invalidate(chat_id)
        delay = self.wait_seconds if chat_id in self.human_chats else 0
        deadline = asyncio.get_running_loop().time() + delay
        # Keep only this process's pending work, never rebuild from DB history.
        # Allow a short reconnect grace AFTER the original deadline, not a new
        # full human wait and not an unbounded replay after a long outage.
        self.jobs[chat_id] = {
            "reply": reply, "deadline": deadline, "expires": deadline + 300,
            "timestamp_ms": timestamp_ms, "sending": False,
        }
        due = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + delay))
        logger.info(f"{tag} 已排队：等待 {delay} 秒；截止 {due}；"
                    f"{'新消息替换原任务；' if replaced else ''}"
                    f"{'断线暂停中' if self.suspended else '连接可用'}")
        if not self.suspended:
            return self._start(chat_id)
        return None

    def _start(self, chat_id):
        job = self.jobs[chat_id]
        version = self.versions[chat_id]
        tag = f"会话 {chat_id} 消息 {job['timestamp_ms']}"
        loop = asyncio.get_running_loop()

        def current():
            return (not self.suspended and self.versions.get(chat_id) == version
                    and self.jobs.get(chat_id) is job and loop.time() <= job["expires"])

        async def run():
            try:
                delay = max(0, job["deadline"] - loop.time())
                if delay:
                    await asyncio.sleep(delay)
                if current():
                    logger.info(f"{tag} 等待结束，开始处理")
                    await job["reply"](current)
                else:
                    logger.info(f"{tag} 不回复：任务已失效或过期")
            except asyncio.CancelledError:
                logger.info(f"{tag} 本轮执行已取消：人工介入、新消息或连接切换")
                raise
            except Exception as exc:
                # Only a type/code: exception strings may contain credentials.
                logger.error(f"{tag} 自动回复任务失败：{type(exc).__name__}；未重试发送")
            finally:
                if self.pending.get(chat_id) is asyncio.current_task():
                    self.pending.pop(chat_id, None)
                    if self.jobs.get(chat_id) is job:
                        self.jobs.pop(chat_id, None)

        task = asyncio.create_task(run())
        self.pending[chat_id] = task
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def mark_sending(self, chat_id):
        """Once socket submission starts its delivery is uncertain: never replay."""
        if chat_id in self.jobs:
            self.jobs[chat_id]["sending"] = True

    def suspend(self):
        """Invalidate old executions but retain unsent jobs and their deadlines."""
        self.suspended = True
        for chat_id, job in list(self.jobs.items()):
            self.versions[chat_id] = self.versions.get(chat_id, 0) + 1
            task = self.pending.pop(chat_id, None)
            if task is not None:
                task.cancel()
            if job["sending"]:
                self.jobs.pop(chat_id, None)
                logger.warning(f"会话 {chat_id} 断线时发送结果不确定，不自动重发")
        logger.info(f"连接断开：保留 {len(self.jobs)} 个未发送任务，等待重连")

    def resume(self):
        if not self.suspended:
            return
        self.suspended = False
        now = asyncio.get_running_loop().time()
        for chat_id, job in list(self.jobs.items()):
            if now > job["expires"]:
                logger.info(f"会话 {chat_id} 消息 {job['timestamp_ms']} 不恢复：超过原截止时间5分钟")
                self.invalidate(chat_id)
                continue
            logger.info(f"会话 {chat_id} 消息 {job['timestamp_ms']} 重连恢复："
                        f"剩余等待 {max(0, job['deadline'] - now):.1f} 秒")
            self._start(chat_id)

    async def close(self):
        self.suspended = True
        for chat_id in list(self.jobs):
            self.invalidate(chat_id)
        tasks = list(self.tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
