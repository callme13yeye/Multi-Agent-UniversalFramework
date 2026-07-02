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
        """主监听循环。"""
        try:
            async for changes in awatch(self._subagents_dir):
                if self._stop_event.is_set():
                    break
                await self._handle_changes(changes)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("[SubAgentHotReload] 监听循环异常退出")

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
