# app/gateway/health_probe.py — 后台模型健康探活
"""后台定期对所有注册模型发送 ping，自动更新健康状态和熔断器。

探活成功 → 熔断器自动恢复（HALF_OPEN → CLOSED）
探活失败 → 累计错误，可能触发熔断
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.gateway.model_gateway import ModelGateway

logger = logging.getLogger(__name__)


class HealthProbe:
    """后台健康探活器。

    定期（默认 30 秒）对所有已注册模型发送轻量 ping 消息，
    记录延迟和成败。对于处于 HALF_OPEN 状态的熔断器，
    探活成功会自动将其恢复为 CLOSED。
    """

    def __init__(self, gateway: ModelGateway, interval_seconds: float = 30.0) -> None:
        self._gateway = gateway
        self._interval = interval_seconds

    async def run(self) -> None:
        """主循环，每 interval_seconds 秒执行一次。"""
        logger.info("[HealthProbe] 探活循环已启动，间隔 %.0fs", self._interval)
        while True:
            # 分片 sleep（每秒检查一次取消信号），避免 shutdown 时最多等 30 秒
            for _ in range(int(self._interval)):
                await asyncio.sleep(1)
            await self._probe_all()

    async def _probe_all(self) -> None:
        """对所有已注册模型执行一次探活。"""
        # 快照当前模型列表（避免在迭代过程中因锁而变化）
        names = list(self._gateway._models.keys())
        if not names:
            return

        tasks = [self._probe_one(name) for name in names]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _probe_one(self, name: str) -> None:
        """对单个模型的所有接口实例发送 ping 探测。"""
        ifaces = self._gateway._models.get(name)
        if ifaces is None:
            return

        PROBE_TIMEOUT = 10.0  # 单次探活超时（秒），防止无响应 API 阻塞 shutdown

        for iface, model in list(ifaces.items()):
            try:
                start = time.monotonic()
                # 优先尝试 ainvoke（LangChain 接口）
                if hasattr(model, "ainvoke"):
                    await asyncio.wait_for(
                        model.ainvoke([{"role": "user", "content": "ping"}]),
                        timeout=PROBE_TIMEOUT,
                    )
                # 备选 acomplete（LlamaIndex 接口）
                elif hasattr(model, "acomplete"):
                    await asyncio.wait_for(
                        model.acomplete("ping"),
                        timeout=PROBE_TIMEOUT,
                    )
                else:
                    logger.debug("[HealthProbe] %s/%s: 无可探测接口，跳过", name, iface)
                    continue

                latency = (time.monotonic() - start) * 1000
                await self._gateway.record_success(name, latency)
                logger.debug("[HealthProbe] %s/%s: ✅ %.0fms", name, iface, latency)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                await self._gateway.record_failure(name, str(e))
                logger.debug("[HealthProbe] %s/%s: ❌ %s", name, iface, e)
