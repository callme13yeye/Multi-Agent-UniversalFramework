# app/gateway/model_gateway.py — 模型智能网关核心
"""模型智能网关单例：注册表、健康跟踪、智能路由、热切换。

所有模型生命周期由此统一管理。消费者通过 ``get_model_chain(role)``
获取健康排序的模型链，无需关心底层切换细节。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Optional

from app.gateway.types import CircuitState, HealthRecord, ModelRole, ModelSpec
from app.gateway.circuit_breaker import CircuitBreaker

logger = logging.getLogger(__name__)


class ModelGateway:
    """模型智能网关 — 中心单例。

    职责:
        1. 模型注册 — 启动时从 config 加载所有模型
        2. 健康跟踪 — 记录每次调用的延迟/成败
        3. 熔断管理 — 自动/手动触发与恢复
        4. 智能路由 — 返回健康排序的模型链
        5. 热切换   — 运行时无停机更换活跃模型

    线程安全:
        - 写操作（register / set_active / invalidate）受 ``_lock`` 保护
        - 读操作（get_model_chain / get_all_status）无锁 — Python 对象引用赋值是原子的
        - HealthRecord 字段使用单独的 per-model 锁保护 latency_samples

    Usage:
        gateway = ModelGateway()
        await gateway.register_model(spec, instance)
        await gateway.start_probe(interval_seconds=30)

        # 消费者获取模型链
        for name, model in gateway.get_model_chain(ModelRole.CHAT):
            ...
    """

    def __init__(self) -> None:
        # ── 注册表 ──
        # name → {interface: LLM 实例}  — 同一模型可有 LangChain + LlamaIndex 双接口
        self._models: dict[str, dict[str, Any]] = {}
        self._specs: dict[str, ModelSpec] = {}     # name → ModelSpec
        self._health: dict[str, HealthRecord] = {}  # name → HealthRecord
        self._breakers: dict[str, CircuitBreaker] = {}  # name → CircuitBreaker

        # ── 路由 ──
        self._active: dict[ModelRole, str] = {}          # role → primary model name
        self._fallback_chains: dict[ModelRole, list[str]] = {}  # role → [name, ...]

        # ── 并发控制 ──
        self._lock = asyncio.Lock()
        self._health_locks: dict[str, asyncio.Lock] = {}  # per-model 锁，保护 HealthRecord 并发写入

        # ── 后台探活 ──
        self._probe_task: Optional[asyncio.Task[None]] = None

    # ═══════════════════════════════════════════════════════════
    # 注册
    # ═══════════════════════════════════════════════════════════

    async def register_model(self, spec: ModelSpec, instance: Any) -> None:
        """注册一个模型实例。

        同一模型名可注册多次（对应不同角色/接口），
        网关自动按接口类型（langchain / llama_index）分存。

        Args:
            spec: 模型规格描述。
            instance: 已加载的 LLM 实例（LangChain 或 LlamaIndex 接口）。
        """
        async with self._lock:
            # ── 按接口类型分存 ──────────────────────────────
            if spec.name not in self._models:
                self._models[spec.name] = {}
            # 从第一个角色推导所需的接口类型
            iface = spec.roles[0].interface if spec.roles else "langchain"
            self._models[spec.name][iface] = instance

            # ── 合并 spec roles（同名模型多次注册时追加角色） ──
            if spec.name not in self._specs:
                self._specs[spec.name] = spec
            else:
                existing = self._specs[spec.name]
                for role in spec.roles:
                    if role not in existing.roles:
                        existing.roles.append(role)

            if spec.name not in self._health:
                self._health[spec.name] = HealthRecord()
            if spec.name not in self._breakers:
                self._breakers[spec.name] = CircuitBreaker()
            if spec.is_primary:
                for role in spec.roles:
                    self._active[role] = spec.name
            logger.info(
                "[Gateway] 注册模型: %s (provider=%s, roles=%s, interface=%s, primary=%s)",
                spec.name,
                spec.provider,
                [r.value for r in spec.roles],
                iface,
                spec.is_primary,
            )

    def set_fallback_chain(self, role: ModelRole, chain: list[str]) -> None:
        """设置某角色的降级链（按顺序尝试）。"""
        self._fallback_chains[role] = chain
        logger.info("[Gateway] 设置降级链: role=%s chain=%s", role.value, chain)

    # ═══════════════════════════════════════════════════════════
    # 路由（无锁读）
    # ═══════════════════════════════════════════════════════════

    def get_model_chain(self, role: ModelRole) -> list[tuple[str, Any]]:
        """获取某角色的健康排序模型链。

        自动根据角色选择正确的接口类型（LangChain / LlamaIndex）。

        返回 (模型名, 模型实例) 列表，按以下顺序排列:
            1. 当前活跃模型（如果熔断器未 OPEN）
            2. 降级链中的模型（跳过熔断器 OPEN 的）
            3. 兜底：即使熔断也包含活跃模型（宁可失败也不错失）

        Consumer 应遍历此链直到成功。
        """
        iface = role.interface
        result: list[tuple[str, Any]] = []
        seen: set[str] = set()

        primary_name = self._active.get(role)
        if primary_name and primary_name in self._models:
            instance = self._models[primary_name].get(iface)
            if instance is not None:
                cb = self._breakers.get(primary_name)
                if cb is None or not cb.is_open():
                    result.append((primary_name, instance))
                    seen.add(primary_name)

        for name in self._fallback_chains.get(role, []):
            if name in seen or name not in self._models:
                continue
            instance = self._models[name].get(iface)
            if instance is None:
                continue
            cb = self._breakers.get(name)
            if cb is not None and cb.is_open():
                continue
            result.append((name, instance))
            seen.add(name)

        # 兜底：活跃模型即使熔断也加入
        if primary_name and primary_name not in seen and primary_name in self._models:
            instance = self._models[primary_name].get(iface)
            if instance is not None:
                result.append((primary_name, instance))

        return result

    def get_instance(self, name: str, interface: str = "langchain") -> Optional[Any]:
        """按名称 + 接口类型获取模型实例。

        Args:
            name: 模型名称。
            interface: ``"langchain"`` 或 ``"llama_index"``。
        """
        per_name = self._models.get(name)
        if per_name is None:
            return None
        return per_name.get(interface)

    # ═══════════════════════════════════════════════════════════
    # 健康记录
    # ═══════════════════════════════════════════════════════════

    async def record_success(self, name: str, latency_ms: float) -> None:
        """记录一次成功的模型调用。

        使用 per-model 锁保护 HealthRecord 的写入，防止
        GatewayMiddleware（请求路径）和 HealthProbe（后台探活）并发
        修改同一模型的健康数据时产生竞态。
        """
        hr = self._health.get(name)
        if hr is None:
            return

        # ── 获取或创建 per-model 锁 ──
        if name not in self._health_locks:
            self._health_locks[name] = asyncio.Lock()
        lock = self._health_locks[name]

        async with lock:
            hr.total_requests += 1
            hr.consecutive_errors = 0
            hr.last_success_ts = time.time()
            hr.add_latency(latency_ms)

        # 熔断器回调在锁外执行（CircuitBreaker 有自己的内部锁）
        cb = self._breakers.get(name)
        if cb is not None:
            await cb.on_success()

    async def record_failure(self, name: str, error_message: str) -> None:
        """记录一次失败的模型调用，可能触发自动熔断。

        使用 per-model 锁保护 HealthRecord 的写入。
        """
        hr = self._health.get(name)
        if hr is None:
            return

        if name not in self._health_locks:
            self._health_locks[name] = asyncio.Lock()
        lock = self._health_locks[name]

        async with lock:
            hr.total_requests += 1
            hr.total_errors += 1
            hr.consecutive_errors += 1
            hr.last_error_ts = time.time()
            hr.last_error_message = error_message

        # 熔断器回调在锁外执行（CircuitBreaker 有自己的内部锁）
        cb = self._breakers.get(name)
        if cb is not None:
            await cb.on_failure()  # CircuitBreaker 内部自增计数 + 判断是否熔断

    # ═══════════════════════════════════════════════════════════
    # 热切换
    # ═══════════════════════════════════════════════════════════

    async def set_active_model(self, role: ModelRole, name: str) -> None:
        """零停机热切换某角色的活跃模型。

        所有进行中的请求不受影响；下一个请求将使用新模型。
        """
        async with self._lock:
            if name not in self._models:
                raise ValueError(f"未知模型: {name}")
            if role.interface not in self._models[name]:
                raise ValueError(
                    f"模型 {name} 未注册 {role.interface} 接口，无法用于 {role.value} 角色"
                )
            self._active[role] = name
            # 重置熔断器
            cb = self._breakers.get(name)
            if cb is not None:
                await cb.reset()
            logger.info(
                "[Gateway] 热切换: role=%s → %s",
                role.value,
                name,
            )

    # ═══════════════════════════════════════════════════════════
    # 状态查询
    # ═══════════════════════════════════════════════════════════

    def get_all_status(self) -> dict[str, Any]:
        """返回所有模型的完整状态（供管理 API 使用）。"""
        result: dict[str, Any] = {}
        # 遍历 _models（name → {interface: instance}）
        for name, ifaces in self._models.items():
            spec = self._specs.get(name)
            hr = self._health.get(name)
            cb = self._breakers.get(name)
            roles = [
                role.value
                for role, active_name in self._active.items()
                if active_name == name
            ]
            result[name] = {
                "spec": {
                    "name": spec.name if spec else name,
                    "provider": spec.provider if spec else "unknown",
                    "roles": [r.value for r in spec.roles] if spec else [],
                    "enabled": spec.enabled if spec else True,
                    "interfaces": list(ifaces.keys()),  # 已注册的接口类型
                },
                "health": {
                    "total_requests": hr.total_requests if hr else 0,
                    "total_errors": hr.total_errors if hr else 0,
                    "error_rate": round(hr.error_rate, 4) if hr else 0.0,
                    "consecutive_errors": hr.consecutive_errors if hr else 0,
                    "last_latency_ms": round(hr.last_latency_ms, 2) if hr else 0,
                    "p50_latency_ms": round(hr.p50_latency_ms, 2) if hr else 0,
                    "p95_latency_ms": round(hr.p95_latency_ms, 2) if hr else 0,
                    "is_healthy": hr.is_healthy if hr else False,
                    "last_error_message": hr.last_error_message if hr else "",
                },
                "circuit": {
                    "state": cb.state.value if cb else "unknown",
                },
                "roles": roles,
            }
        return result

    # ═══════════════════════════════════════════════════════════
    # 后台探活
    # ═══════════════════════════════════════════════════════════

    async def start_probe(self, interval_seconds: float = 30.0) -> None:
        """启动后台健康探活任务。"""
        if self._probe_task is not None:
            return
        from app.gateway.health_probe import HealthProbe

        self._probe_task = asyncio.create_task(
            HealthProbe(self, interval_seconds).run()
        )
        logger.info("[Gateway] 健康探活已启动 (interval=%ss)", interval_seconds)

    async def stop_probe(self) -> None:
        """停止后台健康探活任务。"""
        if self._probe_task is not None:
            self._probe_task.cancel()
            try:
                await self._probe_task
            except asyncio.CancelledError:
                pass
            self._probe_task = None
            logger.info("[Gateway] 健康探活已停止")
