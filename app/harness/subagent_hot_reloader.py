# subagent_hot_reloader.py — SubAgent 目录热加载器
#
# 使用 watchfiles 监听 app/subagents/ 目录，检测到 AGENT.md 文件新增/修改/删除时
# 自动触发 Agent 重建（重新发现 Specialist Agent）。
#
# 与 ToolHotReloader 的区别：
#   - 无需模块重载 — SubAgent 通过 AGENT.md 声明，discover_specialist_agents() 扫描即可
#   - 只监听 AGENT.md 文件 — 忽略其他文件

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Callable, Awaitable

from watchfiles import awatch

logger = logging.getLogger(__name__)


class SubAgentHotReloader:
    """监听 app/subagents/ 目录，AGENT.md 变更时自动重建 Agent。"""

    def __init__(
        self,
        subagents_dir: Path,
        on_reload: Callable[[], Awaitable[None]],
    ):
        self._subagents_dir = subagents_dir
        self._on_reload = on_reload
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        """启动后台文件监听任务。"""
        if self._task is not None:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._watch_loop())
        logger.info("[SubAgentHotReload] 已启动，监听 %s", self._subagents_dir)

    async def stop(self) -> None:
        """停止后台文件监听任务。"""
        if self._task is None:
            return
        self._stop_event.set()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        logger.info("[SubAgentHotReload] 已停止")

    async def _watch_loop(self) -> None:
        """Main watch loop with auto-reconnect on transient errors."""
        retry_delay = 1.0
        max_retry_delay = 30.0

        while not self._stop_event.is_set():
            try:
                async for changes in awatch(self._subagents_dir):
                    if self._stop_event.is_set():
                        break
                    await self._handle_changes(changes)
                break
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception(
                    "[SubAgentHotReload] watch loop error, retrying in %.1fs", retry_delay,
                )
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=retry_delay,
                    )
                    break
                except asyncio.TimeoutError:
                    retry_delay = min(retry_delay * 2, max_retry_delay)

    async def _handle_changes(
        self, changes: set[tuple[int, str]]
    ) -> None:
        """处理文件变更。只关注 AGENT.md 文件。"""
        has_agent_md_change = any(
            Path(path_str).name == "AGENT.md"
            for _change_type, path_str in changes
        )
        if not has_agent_md_change:
            return

        logger.info("[SubAgentHotReload] 检测到 AGENT.md 变更，触发 Agent 重建")
        try:
            await self._on_reload()
        except Exception:
            logger.exception("[SubAgentHotReload] ❌ Agent 重建失败")
