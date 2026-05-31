# ================== 技能工具自动发现 + 全链路追踪 ==========
# 新增部门技能时只需创建 app/skills/<name>/tools.py，
# 写 @tool 装饰的函数，系统自动扫描注册并注入 Langfuse 追踪。
# 零配置、零注册、零 import 修改 —— 真正的插件化架构。
# ==========================================================
import importlib
import logging
from pathlib import Path

from langchain_core.tools import BaseTool
from langfuse import observe

logger = logging.getLogger(__name__)


def discover_skill_tools() -> list[BaseTool]:
    """
    自动扫描 app/skills/<name>/tools.py，收集所有 @tool 装饰的工具。
    同时自动注入 Langfuse observe() 追踪，无需在每个 skill 中手动添加。
    """
    tools: list[BaseTool] = []
    skills_dir = Path(__file__).parent

    for child in sorted(skills_dir.iterdir()):
        if not child.is_dir() or child.name.startswith("_"):
            continue
        tools_file = child / "tools.py"
        if not tools_file.exists():
            continue

        module_name = f"app.skills.{child.name}.tools"
        try:
            mod = importlib.import_module(module_name)
        except Exception as e:
            logger.warning(f"加载技能工具模块失败: {module_name} — {e}")
            continue

        for attr_name in dir(mod):
            obj = getattr(mod, attr_name, None)
            if isinstance(obj, BaseTool):
                # === Langfuse 全链路追踪自动注入 ===
                # 工具定义时写了 @tool，@observe()
                # 这样所有 skill 工具无需手动添加追踪装饰器
                if obj.coroutine and not getattr(obj.coroutine, '_langfuse_observed', False):
                    obj.coroutine = observe()(obj.coroutine)
                    obj.coroutine._langfuse_observed = True  # 标记防重复包装
                # ==================================
                tools.append(obj)
                logger.info(f"发现技能工具: {obj.name} ← {module_name}")

    return tools
