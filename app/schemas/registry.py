"""Schema 注册中心 — 管理 Specialist Agent 输出 Schema 的注册、查找、验证。

核心设计：
    - 每个 Specialist 通过 AGENT.md 的 ``output_schema`` 声明输出的 JSON Schema
    - SchemaRegistry 将 JSON Schema 转为 Pydantic 模型，供 DeepAgents 的
      ``response_format`` (ToolStrategy) 消费
    - 验证失败时错误信息可供 LLM 重试

与 Claude Code 的 StructuredOutput 机制一致：
    注入工具 → 强制 tool_choice="required" → 验证 → 失败重试 → 返回类型化结果
    此机制由 DeepAgents 的 ToolStrategy 实现，本模块负责 Schema 管理。

使用方式::

    from app.schemas import schema_registry, get_schema

    model = schema_registry.from_json_schema("talent_search", schema_dict)
    validated = schema_registry.validate("talent_search", raw_data)
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, create_model, ValidationError

logger = logging.getLogger(__name__)


class SchemaRegistry:
    """Schema 注册中心 — Pydantic 模型的管理与验证。

    支持两种注册方式：
    1. 从 JSON Schema dict 动态创建 Pydantic 模型（from_json_schema）
    2. 直接注册已有的 Pydantic 模型（register）
    """

    def __init__(self):
        self._models: dict[str, type[BaseModel]] = {}

    # ── 注册 ──────────────────────────────────────────────

    def register(self, name: str, model: type[BaseModel]):
        """注册一个 Pydantic 模型。

        Args:
            name: 模型名（与 AGENT.md 的 output_schema 引用名对应）
            model: Pydantic BaseModel 子类

        Raises:
            ValueError: 如果同名模型已存在
        """
        if name in self._models:
            existing = self._models[name]
            raise ValueError(
                f"Schema 名冲突: '{name}' 已被注册 "
                f"(现有: {existing.__name__})"
            )
        self._models[name] = model
        logger.info("[SchemaRegistry] 已注册: %s → %s", name, model.__name__)

    def get(self, name: str) -> type[BaseModel] | None:
        """查找已注册的模型。"""
        return self._models.get(name)

    # ── JSON Schema → Pydantic ─────────────────────────────

    def from_json_schema(
        self,
        name: str,
        schema: dict[str, Any],
    ) -> type[BaseModel]:
        """从 JSON Schema dict 动态创建 Pydantic 模型。

        使用 pydantic.create_model 将 JSON Schema 的 properties
        映射为 Pydantic 字段。

        Args:
            name: 模型名（用作 Pydantic 类名）
            schema: JSON Schema dict，需包含 type/properties/required

        Returns:
            动态创建的 Pydantic BaseModel 子类

        Raises:
            ValueError: Schema 格式无效
        """
        # 缓存命中 — 同一名称直接返回已有模型
        existing = self._models.get(name)
        if existing is not None:
            return existing

        if schema.get("type") != "object":
            raise ValueError(
                f"Schema '{name}': 仅支持 type=object，当前为 {schema.get('type')}"
            )

        properties = schema.get("properties", {})
        required: set[str] = set(schema.get("required", []))

        if not properties:
            raise ValueError(f"Schema '{name}': properties 为空")

        fields: dict[str, tuple[type, Any]] = {}
        for prop_name, prop_schema in properties.items():
            python_type, default = self._json_type_to_python(prop_schema)
            if prop_name not in required:
                # 非必填字段使用 None 作为默认值
                fields[prop_name] = (python_type | None, None)
            else:
                fields[prop_name] = (python_type, ...)

        model = create_model(
            name,
            **fields,
        )
        # 缓存
        self._models[name] = model
        logger.info(
            "[SchemaRegistry] 从 JSON Schema 创建模型: %s (%d 个字段, %d 必填)",
            name, len(fields), len(required),
        )
        return model

    # ── 验证 ──────────────────────────────────────────────

    def validate(
        self,
        name: str,
        data: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, list[str] | None]:
        """验证数据是否符合指定 Schema。

        Args:
            name: 模型名
            data: 待验证的原始数据

        Returns:
            (validated_data, errors) — 成功时 errors 为 None；
            失败时 validated_data 为 None，errors 为描述列表
        """
        model = self._models.get(name)
        if model is None:
            return None, [f"未知 Schema: '{name}'"]

        try:
            validated = model.model_validate(data)
            return validated.model_dump(), None
        except ValidationError as e:
            errors = [
                f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
                for err in e.errors()
            ]
            logger.warning(
                "[SchemaRegistry] 验证失败: %s — %d 个错误", name, len(errors),
            )
            return None, errors

    def validate_or_raise(self, name: str, data: dict[str, Any]) -> dict[str, Any]:
        """验证数据，失败时抛出 ValidationError。"""
        model = self._models.get(name)
        if model is None:
            raise ValueError(f"未知 Schema: '{name}'")
        return model.model_validate(data).model_dump()

    # ── 内部辅助 ──────────────────────────────────────────

    @staticmethod
    def _json_type_to_python(prop: dict[str, Any]) -> tuple[type, Any]:
        """将 JSON Schema 类型映射为 Python 类型。

        支持 type/items/enum 等常用 JSON Schema 关键字。
        """
        json_type = prop.get("type", "string")

        type_map: dict[str, type] = {
            "string": str,
            "integer": int,
            "number": float,
            "boolean": bool,
            "array": list,
            "object": dict,
        }

        if json_type == "array":
            # 简化处理：数组默认 list[dict] 或 list[str]
            items = prop.get("items", {})
            item_type = items.get("type", "string") if isinstance(items, dict) else "string"
            inner_type = type_map.get(item_type, str)
            return list[inner_type], []

        if json_type in type_map:
            return type_map[json_type], type_map[json_type]()

        logger.warning("[SchemaRegistry] 未知 JSON type: %s，降级为 str", json_type)
        return str, ""


# ── 全局单例 ─────────────────────────────────────────────

schema_registry = SchemaRegistry()


def get_schema(name: str) -> type[BaseModel] | None:
    """获取注册的 Schema 模型。"""
    return schema_registry.get(name)


def register_schema(name: str, model: type[BaseModel]):
    """装饰器/函数：注册一个 Pydantic 模型。"""
    schema_registry.register(name, model)
