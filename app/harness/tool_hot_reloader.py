# tool_hot_reloader.py — Tool 目录热加载器
#
# 使用 watchfiles 监听 app/tools/ 目录，检测到 .py 文件新增/修改/删除时
# 自动重载/清理模块并重建 Agent，无需重启服务或手动触发。
#
# 排除规则：
#   - _ 前缀的文件（_registry.py 等私有模块）
#   - resources.py（非工具基础设施，由 main.py 显式管理）
#
# 防抖：watchfiles 内置变更合并，同一批次变更只会触发一次回调。

from __future__ import annotations

import asyncio
import importlib
import logging
import sys
from pathlib import Path
from typing import Callable, Awaitable

from watchfiles import awatch, Change

logger = logging.getLogger(__name__)


class ToolHotReloader:
    """监听 app/tools/ 目录，文件变更时自动重载工具并重建 Agent。"""

    def __init__(
        self,
        tools_dir: Path,
        on_reload: Callable[[], Awaitable[None]],
    ):
        self._tools_dir = tools_dir
        self._on_reload = on_reload
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        """启动后台文件监听任务。"""
        if self._task is not None:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._watch_loop())
        logger.info("[HotReload] 工具热加载器已启动，监听 %s", self._tools_dir)

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
        logger.info("[HotReload] 工具热加载器已停止")

    async def _watch_loop(self) -> None:
        """Main watch loop with auto-reconnect on transient errors."""
        retry_delay = 1.0
        max_retry_delay = 30.0

        while not self._stop_event.is_set():
            try:
                async for changes in awatch(self._tools_dir):
                    if self._stop_event.is_set():
                        break
                    await self._handle_changes(changes)
                # awatch exited normally (unlikely, but handle)
                break
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception(
                    "[HotReload] watch loop error, retrying in %.1fs", retry_delay,
                )
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=retry_delay,
                    )
                    break  # stop was set
                except asyncio.TimeoutError:
                    retry_delay = min(retry_delay * 2, max_retry_delay)

    async def _handle_changes(
        self, changes: set[tuple[int, str]]
    ) -> None:
        """处理一批文件变更。只处理 .py 工具模块。"""
        reloaded = False

        for change_type, path_str in changes:
            path = Path(path_str)
            if path.suffix != ".py":
                continue

            name = path.stem
            # 跳过私有模块和非工具基础设施
            if name.startswith("_") or name == "resources":
                continue

            module_name = f"app.tools.{name}"

            try:
                from app.tools._registry import unregister_module, _TOOL_SOURCES, TOOL_REGISTRY

                # ── 文件删除：只清理注册 + sys.modules，不重新导入 ──
                if change_type == Change.deleted:
                    removed = unregister_module(module_name)
                    if removed:
                        logger.info(
                            "[HotReload] 🗑 工具文件已删除，清理注册: %s → %s",
                            module_name, removed,
                        )
                    if module_name in sys.modules:
                        del sys.modules[module_name]
                    reloaded = True
                    continue

                # ── 文件新增/修改：先重载/导入，成功后再清理旧工具 ──
                # 关键：必须先成功重载再清理。如果先 unregister 再 reload 失败，
                # 旧工具已清除但新工具没注册成功 → 工具永久丢失。
                old_tool_names = [
                    name for name, src in _TOOL_SOURCES.items()
                    if src == module_name
                ]

                try:
                    if module_name in sys.modules:
                        importlib.reload(sys.modules[module_name])
                    else:
                        importlib.import_module(module_name)

                    reloaded = True
                    logger.info("[HotReload] ✅ 工具模块已重载: %s", module_name)
                except Exception:
                    logger.exception(
                        "[HotReload] ❌ 重载失败，旧工具保持不变: %s", module_name
                    )
                    # 重载失败 — 旧工具原封不动，跳过清理
                    continue

                # ── 清理旧版本有但新版本没有的"僵尸"工具 ──
                # register_tool 已处理同名工具的覆盖（同一模块允许覆盖），
                # 这里只需清理模块不再导出的工具。
                new_tool_names = [
                    name for name, src in _TOOL_SOURCES.items()
                    if src == module_name
                ]
                stale_names = set(old_tool_names) - set(new_tool_names)
                for name in stale_names:
                    del TOOL_REGISTRY[name]
                    del _TOOL_SOURCES[name]
                    logger.info(
                        "[HotReload] 🧹 清理旧工具: %s (已从 %s 中移除)",
                        name, module_name,
                    )

            except Exception:
                logger.exception("[HotReload] ❌ 处理变更失败: %s", path_str)

        if reloaded and self._on_reload is not None:
            try:
                await self._on_reload()
            except Exception:
                logger.exception("[HotReload] ❌ Agent 重建失败")
