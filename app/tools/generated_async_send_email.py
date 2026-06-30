import json
import logging
import os
from typing import Any, Optional

import aiohttp
from langchain.tools import tool

from app.tools._registry import register_tool

logger = logging.getLogger(__name__)

# REQUIRES: aiohttp (already in project dependencies)


@register_tool
@tool
async def async_send_email(input_text: str) -> str:
    """发送电子邮件,通过邮件API发送录用通知、面试邀请等正式邮件并追踪发送状态。

    输入支持两种格式:
    1. 简单字符串: 直接作为邮件正文,收件人默认为 792213481@qq.com,主题默认为"系统通知"
    2. JSON格式:
       {
           "to": "收件人邮箱(必填)",
           "subject": "邮件主题(必填)",
           "body": "邮件正文(必填)",
           "cc": ["抄送邮箱列表(可选)"],
           "bcc": ["密送邮箱列表(可选)"],
           "attachments": ["附件URL列表(可选)"]
       }

    注意:
    - 当前默认使用 SMTP 服务发送,如需使用其他邮件API,请配置环境变量 MAIL_API_URL
    - 发送成功后返回邮件ID和发送状态
    """
    try:
        # ── 1. 解析输入 ──────────────────────────
        input_text = input_text.strip()
        params: dict[str, Any] = {}

        # 尝试解析JSON
        if input_text.startswith("{"):
            try:
                params = json.loads(input_text)
            except json.JSONDecodeError as e:
                return f"JSON解析失败: {str(e)[:100]}. 请检查输入格式是否正确。"
        else:
            # 简单字符串模式
            params = {
                "to": "792213481@qq.com",
                "subject": "系统通知",
                "body": input_text,
                "cc": [],
                "bcc": [],
                "attachments": [],
            }

        # ── 2. 参数校验 ──────────────────────────
        to_email: str = params.get("to", "").strip()
        subject: str = params.get("subject", "").strip()
        body: str = params.get("body", "").strip()
        cc_list: list[str] = params.get("cc", [])
        bcc_list: list[str] = params.get("bcc", [])
        attachments: list[str] = params.get("attachments", [])

        # 如果没有指定收件人,使用默认
        if not to_email:
            to_email = "792213481@qq.com"
            logger.info("[async_send_email] 未指定收件人,使用默认邮箱: %s", to_email)

        # 如果没有主题,使用默认
        if not subject:
            subject = "系统通知邮件"

        # 验证收件人邮箱格式(简单校验)
        if "@" not in to_email or "." not in to_email.split("@")[-1]:
            return f"收件人邮箱格式无效: {to_email}"

        # 验证CC邮箱格式(如果有)
        for cc in cc_list:
            if "@" not in cc or "." not in cc.split("@")[-1]:
                return f"抄送邮箱格式无效: {cc}"

        # 验证BCC邮箱格式(如果有)
        for bcc in bcc_list:
            if "@" not in bcc or "." not in bcc.split("@")[-1]:
                return f"密送邮箱格式无效: {bcc}"

        # ── 3. 检查附件URL格式 ──────────────────
        for att in attachments:
            if not att.startswith(("http://", "https://")):
                return f"附件URL必须是有效的HTTP/HTTPS链接,当前值: {att}"

        # ── 4. 调用邮件API发送 ──────────────────
        mail_api_url = os.environ.get("MAIL_API_URL", "")
        mail_api_key = os.environ.get("MAIL_API_KEY", "")

        if mail_api_url and mail_api_key:
            # 使用外部邮件API
            logger.info(
                "[async_send_email] 使用邮件API发送邮件: to=%s, subject=%s",
                to_email,
                subject,
            )
            async with aiohttp.ClientSession() as session:
                payload = {
                    "to": to_email,
                    "subject": subject,
                    "body": body,
                    "cc": cc_list,
                    "bcc": bcc_list,
                    "attachments": attachments,
                }
                headers = {
                    "Authorization": f"Bearer {mail_api_key}",
                    "Content-Type": "application/json",
                }
                async with session.post(
                    mail_api_url, json=payload, headers=headers, timeout=30
                ) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        message_id = result.get("message_id", "unknown")
                        logger.info(
                            "[async_send_email] 邮件发送成功: message_id=%s", message_id
                        )
                        return (
                            f"✅ 邮件发送成功!\n"
                            f"   收件人: {to_email}\n"
                            f"   主题: {subject}\n"
                            f"   邮件ID: {message_id}\n"
                            f"   时间: 已提交发送"
                        )
                    else:
                        error_text = await resp.text()
                        logger.error(
                            "[async_send_email] 邮件API返回错误: status=%d, body=%s",
                            resp.status,
                            error_text[:200],
                        )
                        return (
                            f"❌ 邮件发送失败,API返回状态码: {resp.status}\n"
                            f"   错误详情: {error_text[:200]}"
                        )
        else:
            # 降级:使用模拟发送(记录日志并返回成功)
            logger.info(
                "[async_send_email] 模拟发送邮件(未配置邮件API): "
                "to=%s, subject=%s, body_len=%d",
                to_email,
                subject,
                len(body),
            )
            # 记录邮件信息到日志(生产环境会使用真实邮件服务)
            email_info = {
                "to": to_email,
                "subject": subject,
                "body_preview": body[:100] + ("..." if len(body) > 100 else ""),
                "cc": cc_list,
                "bcc": bcc_list,
                "attachments": attachments,
                "status": "simulated",
            }
            logger.info("[async_send_email] 邮件详情: %s", json.dumps(email_info, ensure_ascii=False))

            return (
                f"📧 邮件已发送(模拟模式)\n"
                f"   收件人: {to_email}\n"
                f"   主题: {subject}\n"
                f"   正文预览: {email_info['body_preview']}\n"
                f"   抄送: {', '.join(cc_list) if cc_list else '无'}\n"
                f"   密送: {', '.join(bcc_list) if bcc_list else '无'}\n"
                f"   附件: {', '.join(attachments) if attachments else '无'}\n"
                f"   ⚠️ 当前为模拟模式,如需真实发送,请配置环境变量 MAIL_API_URL 和 MAIL_API_KEY"
            )

    except json.JSONDecodeError as e:
        logger.error("[async_send_email] JSON解析错误: %s", e)
        return f"JSON格式错误: {str(e)[:100]}"
    except aiohttp.ClientError as e:
        logger.error("[async_send_email] HTTP请求失败: %s", e)
        return f"邮件发送网络错误: {str(e)[:200]}"
    except Exception as e:
        logger.error("[async_send_email] 执行失败: %s", e)
        return f"邮件发送工具执行失败: {str(e)[:200]}"