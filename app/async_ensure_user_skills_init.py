# app/async_ensure_user_skills_init.py
from langgraph.store.base import BaseStore
from app.skills.init_skill import DEFAULT_AGENTS_MD

import logging

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
