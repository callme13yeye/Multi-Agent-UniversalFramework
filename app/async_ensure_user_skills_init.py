# app/async_ensure_user_skills_init.py
from langgraph.store.base import BaseStore

import logging

DEFAULT_AGENTS_MD = """# 全局准则
- 始终使用中文回复。
- 回答应简洁、专业，如有不确定请联系管理员。
- 遇到法律、财务等高风险问题时，必须附加免责声明。
"""

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("ensure_user_skills")

async def ensure_user_skills_init(store: BaseStore, user_id: str):
    # 初始化用户记忆文件（全局准则）
    mem_namespace = ("memories", user_id)
    existing_mem = await store.aget(mem_namespace, "/AGENTS.md")
    logger.info(f"检查记忆文件: memories/AGENTS.md 存在={existing_mem is not None}")
    if existing_mem is None:
        await store.aput(mem_namespace, "/AGENTS.md", {"content": DEFAULT_AGENTS_MD, "encoding": "utf-8"})
        logger.info(f"已写入默认记忆文件")
