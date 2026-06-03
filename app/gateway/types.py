# app/gateway/types.py — 模型网关共享类型定义
"""模型网关的共享数据类、枚举和类型定义。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class CircuitState(Enum):
    """熔断器三态。

    CLOSED  → 正常，请求放行
    OPEN    → 熔断，请求拒绝（快速失败）
    HALF_OPEN → 探测，允许少量请求测试恢复
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class ModelRole(Enum):
    """模型在系统中的逻辑角色。

    每个角色对应一类消费者——不同角色可以使用不同模型。
    """

    CHAT = "chat"  # Agent 对话（LangChain ChatDeepSeek / init_chat_model 接口）
    FALLBACK_CHAT = "fallback_chat"  # Agent 备用（LangChain 接口）
    RETRIEVAL_LLM = "retrieval_llm"  # 检索答案生成（LlamaIndex DeepSeek / OpenAILike 接口）
    RETRIEVAL_REWRITER = "retrieval_rewriter"  # Query 改写（LlamaIndex 接口）

    @property
    def interface(self) -> str:
        """返回此角色所需的模型接口类型。

        CHAT / FALLBACK_CHAT 使用 LangChain 接口（用于 Agent 中间件），
        RETRIEVAL_LLM / RETRIEVAL_REWRITER 使用 LlamaIndex 接口（用于检索管线）。
        """
        if self in (ModelRole.CHAT, ModelRole.FALLBACK_CHAT):
            return "langchain"
        return "llama_index"


@dataclass
class ModelSpec:
    """描述一个注册到网关的模型。

    Attributes:
        name: 模型名称，如 ``"deepseek-v4-flash"``。
        provider: 提供商标识，如 ``"deepseek"`` / ``"bailian"``。
        roles: 该模型可扮演的角色列表。
        api_key_env: API Key 环境变量名。
        base_url_env: Base URL 环境变量名。
        is_primary: 是否是其角色的默认模型。
        enabled: 管理员是否启用（可运行时禁用）。
    """

    name: str
    provider: str
    roles: list[ModelRole]
    api_key_env: str
    base_url_env: str
    is_primary: bool = False
    enabled: bool = True


@dataclass
class HealthRecord:
    """单个模型的滚动健康统计。

    所有字段由 ModelGateway.record_success / record_failure
    在无锁路径上更新（Python 基础类型赋值是原子的）。
    ``latency_samples`` 通过 per-model 锁保护。
    """

    total_requests: int = 0
    total_errors: int = 0
    consecutive_errors: int = 0
    last_latency_ms: float = 0.0
    p50_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    last_success_ts: float = 0.0
    last_error_ts: float = 0.0
    last_error_message: str = ""
    latency_samples: list[float] = field(default_factory=list)

    MAX_SAMPLES: int = field(default=100, init=False)

    @property
    def error_rate(self) -> float:
        """错误率 (0.0 ~ 1.0)。"""
        if self.total_requests == 0:
            return 0.0
        return self.total_errors / self.total_requests

    @property
    def is_healthy(self) -> bool:
        """最后一次请求是否成功（最近一次成功晚于最近一次失败）。"""
        return self.last_success_ts > self.last_error_ts

    def add_latency(self, ms: float) -> None:
        """向滚动窗口追加一个延迟样本，并更新 p50/p95。"""
        self.latency_samples.append(ms)
        if len(self.latency_samples) > self.MAX_SAMPLES:
            self.latency_samples.pop(0)
        self.last_latency_ms = ms
        self.p50_latency_ms = _percentile(self.latency_samples, 50)
        self.p95_latency_ms = _percentile(self.latency_samples, 95)


def _percentile(data: list[float], p: float) -> float:
    """计算列表的第 p 百分位数（线性插值）。"""
    if not data:
        return 0.0
    sorted_data = sorted(data)
    k = (len(sorted_data) - 1) * p / 100.0
    f = int(k)
    c = k - f
    if f + 1 < len(sorted_data):
        return sorted_data[f] + c * (sorted_data[f + 1] - sorted_data[f])
    return sorted_data[f]
