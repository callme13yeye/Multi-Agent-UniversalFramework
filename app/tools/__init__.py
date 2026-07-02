# app/tools/ — 工具注册中心
#
# 按域分割的工具集合，支持自动发现。
# 新增工具只需在对应域文件中定义函数并加上 @register_tool 装饰器，
# 无需修改 __init__.py、main.py 或 agent_definitions.py。
#
# 目录结构:
#   _registry.py   — 核心注册机制 (TOOL_REGISTRY + register_tool)
#   resources.py   — 非工具基础设施 (知识库资源、缓存 Key)
#   common.py      — 通用工具 (时间、联网搜索)
#   knowledge.py   — 知识库工具 (RAG 检索)
#   approval.py    — 审批工具 (人审请求，Supervisor 检测后 interrupt 挂起)
#   {domain}.py    — 按域扩展 (hr, finance, engineering, business ...)

import importlib
import logging
from pathlib import Path

# ── 1. 加载核心注册机制 ──────────────────────────────────
from app.tools._registry import TOOL_REGISTRY, register_tool

logger = logging.getLogger(__name__)

# ── 2. 先导入 resources（非工具基础设施，其他工具模块依赖它）──
from app.tools import resources  # noqa: E402

# ── 3. 自动发现并导入所有工具域模块 ──────────────────────
_tools_dir = Path(__file__).parent
_imported = 0

for _file in sorted(_tools_dir.glob("*.py")):
    _name = _file.stem
    # 跳过私有模块和已显式导入的 resources
    if _name.startswith("_") or _name == "resources":
        continue
    try:
        importlib.import_module(f"app.tools.{_name}")
        _imported += 1
    except Exception:
        logger.exception("导入工具模块失败: %s", _name)

logger.info("工具注册中心初始化完成 — %d 个模块, %d 个工具已注册",
            _imported, len(TOOL_REGISTRY))

# ── 4. 统一导出非工具基础设施（供 main.py 等外部使用） ───
from app.tools.resources import (  # noqa: E402, F401
    register_knowledge_resource,
    register_task_executor,
    get_task_executor,
    SOURCES_KEY_PREFIX,
    PENDING_QA_KEY_PREFIX,
)
