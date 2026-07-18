"""审批标记兜底 Hook — P0 安全机制。

当 Executor LLM 连续忽略 Specialist 返回的 ``[HUMAN_APPROVAL_REQUIRED]`` 标记时，
强制抛出 ApprovalNotHandledError，由 TaskExecutor 捕获并转入 WAITING_HUMAN 状态。

替代 TaskExecutor._check_approval_marker_handled() 的硬编码逻辑。

计数器存储在 Store 中（namespace: ``("approval_guard",)``），重启/恢复后不丢失。
"""

from __future__ import annotations

import logging
import re

from langchain.agents.middleware import after_model
from langchain_core.messages import AIMessage, ToolMessage

logger = logging.getLogger(__name__)

APPROVAL_MARKER = "[HUMAN_APPROVAL_REQUIRED]"


def _extract_approval_id(messages: list) -> str:
    """从消息中提取 approval_id。"""
    import json as _json

    for msg in reversed(messages):
        content = str(getattr(msg, "content", ""))
        if APPROVAL_MARKER in content:
            try:
                # 尝试从 JSON 中提取
                data = _json.loads(content)
                aid = data.get("approval_id", "")
                if aid:
                    return aid
            except (_json.JSONDecodeError, TypeError):
                pass
            # 正则兜底
            m = re.search(r'"approval_id"\s*:\s*"([^"]+)"', content)
            if m:
                return m.group(1)
    return ""


async def _get_rounds(store, task_id: str) -> int:
    """从 Store 读取当前累计轮数。"""
    try:
        item = await store.aget(("approval_guard",), f"rounds_{task_id}")
        return item.value.get("rounds", 0) if item and item.value else 0
    except Exception:
        return 0


async def _set_rounds(store, task_id: str, rounds: int) -> None:
    """写入累计轮数到 Store。"""
    try:
        await store.aput(("approval_guard",), f"rounds_{task_id}", {"rounds": rounds})
    except Exception:
        pass


def create_approval_guard_hook(max_rounds: int = 3):
    """创建审批标记兜底 Hook。

    Args:
        max_rounds: 连续未处理轮数阈值。默认 3 轮后抛出异常。
    """

    @after_model
    async def approval_guard(state, runtime):
        """检测 LLM 是否正确处理了审批标记。"""
        ctx = getattr(runtime, "context", None)
        task_id = getattr(ctx, "task_id", "") if ctx else ""
        store = getattr(runtime, "store", None)

        if not task_id or not store:
            return None

        messages = state.get("messages", [])
        if not messages:
            return None

        # ── 检查本轮是否有 request_approval 调用 ──
        has_request_approval = any(
            isinstance(msg, ToolMessage) and getattr(msg, "name", "") == "request_approval"
            for msg in messages[-10:]  # 只看最近 10 条
        )

        if has_request_approval:
            # LLM 正确处理了审批 — 重置计数器
            current_rounds = await _get_rounds(store, task_id)
            if current_rounds > 0:
                logger.info(
                    "[ApprovalGuard] %s: request_approval 已调用，计数器重置 (之前=%d)",
                    task_id, current_rounds,
                )
            await _set_rounds(store, task_id, 0)
            return None

        # ── 检查本轮是否有 [HUMAN_APPROVAL_REQUIRED] 标记 ──
        has_approval_marker = any(
            APPROVAL_MARKER in str(getattr(msg, "content", ""))
            for msg in messages[-10:]
        )

        if not has_approval_marker:
            # 无标记，不增加也不重置（可能还在等 Specialist 返回）
            return None

        # ── 有标记但没 request_approval → 累积 ──
        current = await _get_rounds(store, task_id)
        current += 1
        await _set_rounds(store, task_id, current)

        approval_id = _extract_approval_id(messages)

        logger.warning(
            "[ApprovalGuard] %s: 第 %d 轮未处理审批标记 approval_id=%s",
            task_id, current, approval_id or "N/A",
        )

        if current >= max_rounds:
            from app.harness.task_executor import ApprovalNotHandledError

            raise ApprovalNotHandledError(
                task_id=task_id,
                rounds=current,
                approval_id=approval_id,
            )

        return None

    return approval_guard
