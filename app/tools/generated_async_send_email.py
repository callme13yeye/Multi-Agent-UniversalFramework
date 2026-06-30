# 标准库
import os
import logging
import json
import base64
from typing import Any
from datetime import datetime

# 第三方库(根据需要选择)
import aiohttp  # HTTP 请求(推荐)

# 项目内部(必须)
from app.tools._registry import register_tool
from langchain.tools import tool

logger = logging.getLogger(__name__)

# 邮件发送配置(从环境变量读取)
# 推荐使用企业邮件服务,如 SendGrid, Mailgun, Amazon SES 等
EMAIL_API_URL = os.environ.get("EMAIL_API_URL", "https://api.sendgrid.com/v3/mail/send")
EMAIL_API_KEY = os.environ.get("EMAIL_API_KEY", "")
EMAIL_FROM = os.environ.get("EMAIL_FROM", "noreply@company.com")
EMAIL_FROM_NAME = os.environ.get("EMAIL_FROM_NAME", "HR System")


@register_tool
@tool
async def async_send_email(input_text: str) -> str:
    """发送电子邮件的工具,支持指定收件人、主题、正文和附件,可用于发送Offer邮件等场景。

    输入格式(支持两种方式):
    1. JSON 格式(推荐,支持附件):
       {
           "to": "收件人邮箱,支持多个用逗号分隔",
           "subject": "邮件主题",
           "body": "邮件正文(支持纯文本或HTML格式)",
           "attachments": [
               {
                   "filename": "offer_letter.pdf",
                   "content": "文件内容的Base64编码,
                    或者文件URL(以http://或https://开头)",
                   "type": "application/pdf"
               }
           ],
           "cc": "抄送邮箱(可选,多个用逗号分隔)",
           "bcc": "密送邮箱(可选,多个用逗号分隔)"
       }
    2. 简单字符串格式:
       直接输入收件人邮箱和主题,例如: "send to candidate@example.com with subject Offer Letter"

    返回:
       成功: "邮件已成功发送"
       失败: 详细错误信息

    注意:
       - 附件支持Base64编码的文件内容或文件URL
       - 邮件正文支持纯文本和HTML格式(自动检测)
       - 建议批量发送时使用逗号分隔多个收件人
    """
    try:
        # ── 1. 解析输入 ──────────────────────────
        input_text = input_text.strip()
        
        # 尝试解析 JSON
        try:
            params = json.loads(input_text)
            to_emails = params.get("to", "")
            subject = params.get("subject", "")
            body = params.get("body", "")
            attachments = params.get("attachments", [])
            cc_emails = params.get("cc", "")
            bcc_emails = params.get("bcc", "")
        except json.JSONDecodeError:
            # 简单字符串模式
            logger.info("[async_send_email] 使用简单字符串模式解析输入")
            # 尝试提取"send to X with subject Y"模式
            parts = input_text.split()
            to_emails = ""
            subject = ""
            body = input_text
            attachments = []
            cc_emails = ""
            bcc_emails = ""
            
            # 尝试提取收件人
            for i, part in enumerate(parts):
                if part == "to" and i + 1 < len(parts):
                    to_emails = parts[i + 1].strip()
                if part == "with" and i + 2 < len(parts) and parts[i + 1] == "subject":
                    subject = " ".join(parts[i + 2:]).strip()

        # ── 2. 参数验证 ──────────────────────────
        if not to_emails:
            return "错误: 收件人邮箱不能为空,请提供 'to' 参数"
        
        # 清理收件人列表
        to_list = [email.strip() for email in to_emails.split(",") if email.strip()]
        if not to_list:
            return "错误: 收件人邮箱列表为空"

        # 验证邮箱格式(基本验证)
        for email in to_list:
            if "@" not in email or "." not in email:
                return f"错误: 收件人邮箱 '{email}' 格式不正确"

        # 验证API配置
        if not EMAIL_API_KEY:
            return (
                "错误: 邮件服务未配置(EMAIL_API_KEY为空)。\n\n"
                "请在环境变量中设置:\n"
                "  EMAIL_API_URL: 邮件服务API地址\n"
                "  EMAIL_API_KEY: API密钥\n"
                "  EMAIL_FROM: 发件人邮箱\n"
                "  EMAIL_FROM_NAME: 发件人名称"
            )

        # ── 3. 构建邮件内容 ──────────────────────
        # 检测是否为HTML内容
        is_html = body.strip().startswith("<") and body.strip().endswith(">")
        
        # 处理附件
        processed_attachments = []
        if attachments:
            for idx, attachment in enumerate(attachments):
                if isinstance(attachment, dict):
                    filename = attachment.get("filename", f"attachment_{idx}")
                    content = attachment.get("content", "")
                    file_type = attachment.get("type", "application/octet-stream")
                    
                    # 如果是URL,尝试下载
                    if content.startswith("http://") or content.startswith("https://"):
                        logger.info("[async_send_email] 开始下载附件: %s", filename)
                        try:
                            async with aiohttp.ClientSession() as session:
                                async with session.get(content) as resp:
                                    if resp.status == 200:
                                        content_bytes = await resp.read()
                                        content = base64.b64encode(content_bytes).decode("utf-8")
                                    else:
                                        logger.warning("[async_send_email] 下载附件失败: HTTP %s", resp.status)
                                        continue
                        except Exception as e:
                            logger.warning("[async_send_email] 下载附件异常: %s", e)
                            continue
                    
                    processed_attachments.append({
                        "filename": filename,
                        "content": content,
                        "type": file_type
                    })
                elif isinstance(attachment, str):
                    # 简单的文件路径或URL
                    processed_attachments.append({
                        "filename": os.path.basename(attachment),
                        "content": attachment,
                        "type": "application/octet-stream"
                    })

        # ── 4. 调用邮件服务API ──────────────────
        logger.info(
            "[async_send_email] 准备发送邮件: to=%s, subject=%s, attachments=%d",
            to_emails, subject, len(processed_attachments)
        )

        # 构建SendGrid格式的邮件请求体(可扩展为其他服务)
        mail_data = {
            "personalizations": [
                {
                    "to": [{"email": email} for email in to_list],
                }
            ],
            "from": {
                "email": EMAIL_FROM,
                "name": EMAIL_FROM_NAME
            },
            "subject": subject,
            "content": [
                {
                    "type": "text/html" if is_html else "text/plain",
                    "value": body
                }
            ]
        }

        # 添加抄送
        if cc_emails:
            cc_list = [email.strip() for email in cc_emails.split(",") if email.strip()]
            if cc_list:
                mail_data["personalizations"][0]["cc"] = [
                    {"email": email} for email in cc_list
                ]

        # 添加密送
        if bcc_emails:
            bcc_list = [email.strip() for email in bcc_emails.split(",") if email.strip()]
            if bcc_list:
                mail_data["personalizations"][0]["bcc"] = [
                    {"email": email} for email in bcc_list
                ]

        # 添加附件
        if processed_attachments:
            mail_data["attachments"] = []
            for att in processed_attachments:
                mail_data["attachments"].append({
                    "filename": att["filename"],
                    "content": att["content"],
                    "type": att["type"],
                    "disposition": "attachment"
                })

        # 发送请求
        headers = {
            "Authorization": f"Bearer {EMAIL_API_KEY}",
            "Content-Type": "application/json"
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                EMAIL_API_URL,
                headers=headers,
                json=mail_data,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                response_text = await response.text()
                
                if response.status == 202 or response.status == 200:
                    logger.info(
                        "[async_send_email] 邮件发送成功: to=%s, subject=%s",
                        to_emails, subject
                    )
                    
                    # 构建结果信息
                    result_parts = [
                        f"✅ 邮件已成功发送",
                        f"   📧 收件人: {to_emails}",
                        f"   📝 主题: {subject}"
                    ]
                    
                    if cc_emails:
                        result_parts.append(f"   👥 抄送: {cc_emails}")
                    
                    if processed_attachments:
                        att_names = [att["filename"] for att in processed_attachments]
                        result_parts.append(f"   📎 附件: {', '.join(att_names)}")
                    
                    return "\n".join(result_parts)
                    
                else:
                    logger.error(
                        "[async_send_email] 邮件发送失败: HTTP %s, response=%s",
                        response.status, response_text[:500]
                    )
                    
                    # 处理常见错误
                    if response.status == 401:
                        return "错误: API认证失败,请检查EMAIL_API_KEY是否配置正确"
                    elif response.status == 429:
                        return "错误: 发送频率过高,请稍后重试"
                    else:
                        error_detail = response_text[:200] if response_text else "未知错误"
                        return f"错误: 邮件服务返回状态码 {response.status}, 详情: {error_detail}"

    except aiohttp.ClientError as e:
        logger.error("[async_send_email] 网络请求失败: %s", e)
        return (
            f"❌ 邮件发送失败: 网络连接错误\n"
            f"   🔌 详情: {str(e)[:200]}\n\n"
            f"建议:\n"
            f"1. 检查网络连接是否正常\n"
            f"2. 确认邮件服务API地址({EMAIL_API_URL})是否可访问\n"
            f"3. 稍后重试"
        )
    except json.JSONDecodeError as e:
        logger.error("[async_send_email] JSON解析失败: %s", e)
        return f"错误: 输入格式不正确,请提供有效的JSON字符串或简单文本。详情: {str(e)[:100]}"
    except Exception as e:
        logger.error("[async_send_email] 发送邮件异常: %s", e)
        return f"❌ 邮件发送过程发生异常: {str(e)[:200]}\n\n请稍后重试或联系系统管理员"