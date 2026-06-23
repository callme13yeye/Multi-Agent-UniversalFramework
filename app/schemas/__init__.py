# app/schemas/ — Specialist Agent 输出 Schema 管理
#
# 为 Agent 间通信提供结构化数据验证和类型安全的数据传递。
# 与 DeepAgents 的 response_format (ToolStrategy) 配合使用。
#
# 组件:
#   registry.py — SchemaRegistry + Pydantic 模型工厂 + 验证
#
# 使用方式:
#
#   from app.schemas import schema_registry, get_schema
#
#   # 从 JSON Schema 创建模型
#   model = schema_registry.from_json_schema("talent_search", json_schema_dict)
#
#   # 验证输出
#   data, errors = schema_registry.validate("talent_search", raw_dict)

from app.schemas.registry import SchemaRegistry, schema_registry, get_schema, register_schema

__all__ = [
    "SchemaRegistry",
    "schema_registry",
    "get_schema",
    "register_schema",
]
