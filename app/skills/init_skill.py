# app/skills/init_skill.py

DEFAULT_AGENTS_MD = """# 全局准则
- 始终使用中文回复。
- 回答应简洁、专业，如有不确定请联系管理员。
- 遇到法律、财务等高风险问题时，必须附加免责声明。
"""

# 保留空字典供未来用户自定义技能使用
DEPARTMENT_SKILLS: dict[str, str] = {}
