# _registry.py — 工具注册中心核心机制
# TOOL_REGISTRY + register_tool 装饰器，供 app/tools/__init__.py 和所有工具模块使用。

TOOL_REGISTRY: dict[str, any] = {}
_TOOL_SOURCES: dict[str, str] = {}  # tool_name → module_name，用于热重载时清除旧注册


def register_tool(func):
    """将工具自动注册到 TOOL_REGISTRY，键为 func.name（与 AGENT.md 的 allowed_tools 匹配）。

    热重载时同一模块的工具允许覆盖（不抛异常），跨模块冲突仍报错。

    @tool 装饰器会将 __module__ 改为 langchain_core.tools.structured，
    这里从 func.coroutine / func.func 取回真实的来源模块名。
    """
    # 取回被 @tool 改写之前的真实模块
    original = getattr(func, "coroutine", None) or getattr(func, "func", None) or func
    module_name = original.__module__

    if func.name in TOOL_REGISTRY:
        existing_source = _TOOL_SOURCES.get(func.name, "unknown")
        if existing_source == module_name:
            # 同一模块热重载 — 允许覆盖
            TOOL_REGISTRY[func.name] = func
            _TOOL_SOURCES[func.name] = module_name
            return func
        raise ValueError(
            f"工具名冲突: '{func.name}' 已被注册，"
            f"来源: {existing_source}"
        )
    TOOL_REGISTRY[func.name] = func
    _TOOL_SOURCES[func.name] = module_name
    return func


def unregister_module(module_name: str) -> list[str]:
    """清除某模块注册的所有工具（热重载用）。返回被清除的工具名列表。"""
    to_remove = [name for name, src in _TOOL_SOURCES.items() if src == module_name]
    for name in to_remove:
        del TOOL_REGISTRY[name]
        del _TOOL_SOURCES[name]
    return to_remove
