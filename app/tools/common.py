# common.py — 通用工具
# 时间查询、联网搜索等跨领域通用能力。

import os
import logging
from datetime import datetime

import pytz
from dotenv import load_dotenv
from langchain.tools import tool
from tavily import AsyncTavilyClient

from app.tools._registry import register_tool

load_dotenv("key.env")
tavily_api_key = os.environ.get("TAVILY_API_KEY")
logger = logging.getLogger(__name__)


@register_tool
@tool
async def async_get_current_time() -> str:
    """获取当前精确时间，当时区、日期、时间相关问题时使用此工具"""
    tz = pytz.timezone('Asia/Shanghai')
    current_time = datetime.now(tz)
    return f"当前北京时间：{current_time.strftime('%Y年%m月%d日 %H时%M分%S秒')}， 星期" \
        f"{['一', '二', '三', '四', '五', '六', '日'][current_time.weekday()]}"


@register_tool
@tool
async def async_web_search(question: str) -> str:
    """当问题涉及新闻、天气、实时消息、最新消息或需要联网查询时使用此工具。

    如果联网搜索不可用，会自动降级提示用户基于知识库或通用知识回答。
    """
    try:
        client = AsyncTavilyClient(api_key=tavily_api_key, timeout=15)
        response = await client.search(query=question)
        return response
    except Exception as e:
        logger.warning("[web_search] 联网搜索失败: %s — 降级为告知用户", e)
        return (
            f"⚠️ 联网搜索暂不可用（{str(e)[:100]}）。\n\n"
            f"建议:\n"
            f"1. 如果问题涉及公司内部信息，请使用知识库查询（async_knowledge_query_ask）\n"
            f"2. 如果是通用知识问题，我可以基于已有知识直接回答\n"
            f"3. 稍后重试联网搜索\n\n"
            f"原始问题: {question}"
        )
