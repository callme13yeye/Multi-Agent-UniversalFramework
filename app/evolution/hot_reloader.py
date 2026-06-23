"""evolution/hot_reloader.py — 热加载器。

动态注册新 SubAgent/Tool 到运行中系统，无需重启服务。

核心操作:
1. 将 AGENT.md 从暂存目录迁移到正式目录（app/subagents/{name}/）
2. 重新扫描 subagents + 重建 Triage + Executor agent
3. 原子替换 app.state 中的 agent 引用
4. Git 版本控制（用于回滚）

安全原则:
- 重建过程中不阻塞用户请求（旧 agent 引用继续服务）
- 替换是原子操作（Python 对象引用赋值）
- 所有变更先 git commit，回滚时 git checkout
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from app.evolution.types import EvolutionProposal, ProposalStatus
from app.evolution._state import evolution_state

logger = logging.getLogger(__name__)


class HotReloader:
    """动态注册 SubAgent 和 Tool，无需重启服务。

    与现有架构的集成点:
        - SubAgent: 写入 AGENT.md → discover_specialist_agents() 重新扫描
        - Agent 重建: async_create_agent() 生成新的 CompiledStateGraph
        - TaskExecutor: 更新 executor_agent 引用
        - app.state: 原子替换 agent / executor_agent
    """

    def __init__(self, app_state: Any):
        """app_state 是 FastAPI app.state（starlette.datastructures.State）。

        包含所有运行时资源: agent, executor_agent, task_executor, store, gateway, etc.
        """
        self.app_state = app_state

    # ── Agent 激活 ───────────────────────────────────────

    async def activate_agent(self, proposal: EvolutionProposal) -> bool:
        """激活一个新的 SubAgent。

        步骤:
        1. 检查名称冲突
        2. Git commit 当前状态（以备回滚）
        3. 迁移 AGENT.md 从暂存到正式目录
        4. Git commit 新文件
        5. 重新扫描 subagents
        6. 重建 Triage + Executor agent
        7. 原子替换
        8. 更新 EvolutionState
        """
        agent_name = proposal.agent_name
        if not agent_name:
            raise ValueError("提案缺少 agent_name")

        logger.info("[HotReloader] 开始激活 Agent: %s (proposal=%s)", agent_name, proposal.id)

        # 1. 检查名称冲突
        from app.agent_definitions import discover_specialist_agents
        existing = discover_specialist_agents()
        existing_names = {a.get("name", "") for a in existing}
        if agent_name in existing_names and proposal.status != ProposalStatus.ACTIVE:
            raise ValueError(
                f"Agent '{agent_name}' 已存在。如需替换，请先回滚或删除现有 Agent。"
            )

        # 2. 记录激活前的 Git commit（用于回滚）
        prev_commit = await self._get_current_commit()
        proposal.git_prev_commit = prev_commit

        # 3. 将 AGENT.md 从暂存目录迁移到正式目录
        staging_dir = self._get_staging_dir() / agent_name
        target_dir = self._get_subagents_dir() / agent_name

        if not staging_dir.exists():
            raise FileNotFoundError(f"暂存目录不存在: {staging_dir}")

        # 创建目标目录并复制 AGENT.md
        target_dir.mkdir(parents=True, exist_ok=True)
        staging_agent_md = staging_dir / "AGENT.md"
        target_agent_md = target_dir / "AGENT.md"

        if not staging_agent_md.exists():
            raise FileNotFoundError(f"暂存 AGENT.md 不存在: {staging_agent_md}")

        shutil.copy2(staging_agent_md, target_agent_md)
        logger.info("[HotReloader] AGENT.md 已复制: %s → %s", staging_agent_md, target_dir)

        # 4. Git commit 新文件
        commit_hash = await self._commit_to_git(
            str(target_dir),
            f"evolution: activate SubAgent '{agent_name}' — proposal {proposal.id}"
        )
        proposal.git_commit_hash = commit_hash

        # 5. 重新扫描 subagents
        new_subagents = discover_specialist_agents()
        logger.info(
            "[HotReloader] 重新扫描完成 — %d 个 Specialist（含新增 %s）",
            len(new_subagents), agent_name,
        )

        # 6-7. 重建 Agent 并原子替换
        await self._rebuild_agents(new_subagents)

        # 8. 更新运行时状态
        evolution_state.mark_active(agent_name=agent_name)
        proposal.status = ProposalStatus.ACTIVE
        proposal.activated_at = datetime.now().isoformat()

        # 清除暂存目录中的 AGENT.md（已迁移），但保留 _meta.json 作为记录
        staging_agent_md.unlink(missing_ok=True)
        logger.info("[HotReloader] Agent 激活成功: %s", agent_name)

        return True

    # ── Agent 回滚 ───────────────────────────────────────

    async def rollback_agent(self, proposal: EvolutionProposal) -> bool:
        """回滚一个已激活的 SubAgent。

        策略:
        1. 如果有 git_commit_hash，用 git checkout 恢复该文件
        2. 如果没有（首次新增无历史），直接删除 app/subagents/{name}/
        3. 重新扫描 + 重建 agent
        """
        agent_name = proposal.agent_name
        logger.info("[HotReloader] 开始回滚 Agent: %s (proposal=%s)", agent_name, proposal.id)

        target_dir = self._get_subagents_dir() / agent_name
        prev_commit = proposal.git_prev_commit

        if prev_commit and target_dir.exists():
            # 方案 A: Git 恢复
            await self._git_checkout_file(str(target_dir), prev_commit)
            logger.info("[HotReloader] 通过 Git 恢复: %s → commit %s", target_dir, prev_commit[:8])
        elif target_dir.exists():
            # 方案 B: 直接删除（新增的 Agent，之前不存在）
            shutil.rmtree(target_dir)
            logger.info("[HotReloader] 删除目录: %s", target_dir)
        else:
            logger.warning("[HotReloader] 回滚目标不存在: %s", target_dir)

        # 如果暂存目录还有残留，清理
        staging_dir = self._get_staging_dir() / agent_name
        if staging_dir.exists():
            shutil.rmtree(staging_dir)

        # 重新扫描 + 重建
        from app.agent_definitions import discover_specialist_agents
        new_subagents = discover_specialist_agents()
        await self._rebuild_agents(new_subagents)

        # 更新状态
        evolution_state.mark_inactive(agent_name=agent_name)
        proposal.status = ProposalStatus.ROLLED_BACK
        proposal.deactivated_at = datetime.now().isoformat()

        logger.info("[HotReloader] Agent 回滚完成: %s", agent_name)
        return True

    # ── Agent 重建（核心操作）────────────────────────────

    async def _rebuild_agents(self, new_subagents: list[dict]) -> None:
        """重建 Triage + Executor agent。

        这是整个热加载系统最关键的操作:
        1. 用新的 subagents 列表构建 Triage system prompt
        2. 用新的 subagents 列表构建 Executor system prompt
        3. 调用 async_create_agent 创建新 agent 实例
        4. 原子替换 app.state 中的引用
        5. 更新 TaskExecutor 中的 executor_agent 引用

        影响分析:
        - 进行中的 HTTP 请求持有旧 agent 引用，不受影响
        - 新建操作大约需要 1-3 秒
        - Python 对象引用替换是原子的
        """
        from app.async_create_agent import async_create_agent
        from config import get_config

        config = get_config()
        model_name = config["langchain_chat_model_name"]
        fallback_name = config["fallback_model_name"]

        # 获取 checkpoint/store（从现有 agent 的状态中推断）
        # 这些资源在 app.state 上
        checkpointer = getattr(self.app_state, "checkpointer", None)
        store = getattr(self.app_state, "store", None)
        gateway = getattr(self.app_state, "model_gateway", None)

        # 工具：从 Triage agent 的 system prompt 中获取当前工具列表
        # 工具不变，仍从 TOOL_REGISTRY 获取所有工具
        from app.tools import TOOL_REGISTRY
        tools = list(TOOL_REGISTRY.values())

        logger.info(
            "[HotReloader] 开始重建 Agent — %d 个工具, %d 个 SubAgent",
            len(tools), len(new_subagents),
        )

        # ── 构建 Triage system prompt ──
        from app.prompts.triage_prompt import build_triage_prompt
        triage_prompt = build_triage_prompt(new_subagents)

        # ── 构建 Executor system prompt ──
        from app.prompts.executor_prompt import build_executor_prompt
        executor_prompt = build_executor_prompt(new_subagents)

        # ── 创建新 agent 实例 ──
        new_agent = await async_create_agent(
            model_name=model_name,
            fallback_model_name=fallback_name,
            tools=tools,
            system_prompt=triage_prompt,
            checkpointer=checkpointer,
            store=store,
            subagents=new_subagents,
            gateway=gateway,
        )
        logger.info("[HotReloader] Triage Agent 重建完成")

        new_executor = await async_create_agent(
            model_name=model_name,
            fallback_model_name=fallback_name,
            tools=tools,
            system_prompt=executor_prompt,
            checkpointer=checkpointer,
            store=store,
            subagents=new_subagents,
            gateway=gateway,
        )
        logger.info("[HotReloader] Executor Agent 重建完成")

        # ── 原子替换 ──
        self.app_state.agent = new_agent
        self.app_state.executor_agent = new_executor
        self.app_state.specialist_subagents = new_subagents

        # ── 更新 TaskExecutor 引用 ──
        task_executor = getattr(self.app_state, "task_executor", None)
        if task_executor is not None:
            task_executor.executor_agent = new_executor
            logger.info("[HotReloader] TaskExecutor.executor_agent 已更新")

        logger.info("[HotReloader] Agent 重建完成 — 所有引用已原子替换")

    # ── Git 操作 ─────────────────────────────────────────

    async def _commit_to_git(self, file_path: str, message: str) -> str:
        """提交变更到 Git，返回 commit hash。"""
        import subprocess

        repo_root = self._get_repo_root()
        rel_path = os.path.relpath(file_path, repo_root)

        async def _run_git_cmd(cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
            return await asyncio.to_thread(
                subprocess.run, cmd,
                cwd=repo_root, capture_output=True, timeout=timeout, check=True,
            )

        try:
            await _run_git_cmd(["git", "add", rel_path])
            await _run_git_cmd(["git", "commit", "-m", message])
            hash_result = await _run_git_cmd(["git", "rev-parse", "HEAD"], timeout=10)
            commit_hash = hash_result.stdout.decode().strip()
            logger.info("[HotReloader] Git commit: %s — %s", commit_hash[:8], message)
            return commit_hash

        except subprocess.CalledProcessError as e:
            logger.warning(
                "[HotReloader] Git commit 失败（非致命）: %s — %s",
                e, e.stderr.decode() if e.stderr else "",
            )
            return ""

    async def _git_checkout_file(self, file_path: str, commit_hash: str) -> None:
        """从指定 commit 恢复文件。"""
        import subprocess

        repo_root = self._get_repo_root()
        rel_path = os.path.relpath(file_path, repo_root)

        try:
            await asyncio.to_thread(
                subprocess.run,
                ["git", "checkout", commit_hash, "--", rel_path],
                cwd=repo_root,
                capture_output=True,
                timeout=30,
                check=True,
            )
        except subprocess.CalledProcessError as e:
            logger.error("[HotReloader] Git checkout 失败: %s", e)
            raise RuntimeError(f"Git 恢复失败: {e}")

    async def _get_current_commit(self) -> str:
        """获取当前 HEAD commit hash。"""
        import subprocess

        repo_root = self._get_repo_root()
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                ["git", "rev-parse", "HEAD"],
                cwd=repo_root,
                capture_output=True,
                timeout=10,
                check=True,
            )
            return result.stdout.decode().strip()
        except subprocess.CalledProcessError:
            return ""

    @staticmethod
    def _get_repo_root() -> str:
        """获取 Git repo 根目录。"""
        return str(Path(__file__).parent.parent.parent)

    @staticmethod
    def _get_subagents_dir() -> Path:
        """获取 subagents 正式目录。"""
        return Path(__file__).parent.parent / "subagents"

    @staticmethod
    def _get_staging_dir() -> Path:
        """获取暂存目录。"""
        return Path(__file__).parent.parent / "subagents" / "_staging"
