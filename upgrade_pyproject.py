# 在项目根目录下运行 python
with open("pyproject.toml", encoding="utf-8") as f:
    content = f.read()
content = content.replace(">=", "==")
with open("pyproject.toml", "w", encoding="utf-8") as f:
    f.write(content)