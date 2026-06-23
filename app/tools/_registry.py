# _registry.py — 工具注册中心核心机制
# TOOL_REGISTRY + register_tool 装饰器，供 app/tools/__init__.py 和所有工具模块使用。

TOOL_REGISTRY: dict[str, any] = {}


def register_tool(func):
    """将工具自动注册到 TOOL_REGISTRY，键为 func.name（与 AGENT.md 的 allowed_tools 匹配）。

    如果工具名已存在则抛出 ValueError，防止跨域工具名冲突。
    """
    if func.name in TOOL_REGISTRY:
        raise ValueError(
            f"工具名冲突: '{func.name}' 已被注册，"
            f"来源: {getattr(TOOL_REGISTRY[func.name], '__module__', 'unknown')}"
        )
    TOOL_REGISTRY[func.name] = func
    return func
