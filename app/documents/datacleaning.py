# datacleaning.py
import re
from llama_index.core.schema import TransformComponent

# 自定义数据清洗组件，作为 IngestionPipeline 的一环，这里最好跟业务需求紧密结合，做针对性的清洗。示例中仅做了简单的空白和特殊字符处理。
class DataCleaningComponent(TransformComponent):
    def __call__(self, nodes, **kwargs):
        for node in nodes:
            if hasattr(node, "text"):
                cleaned = self._clean(node.text)
                node.set_content(cleaned) 
        return nodes
    # 针对我提供的简历进行数据清洗
    def _clean(self, text: str) -> str:
        # 移除pdf分页标记
        text = re.sub(r'={3,}\s*Page\s+\d+\s*={3,}', '', text)
        # 移除多余的控制字符（零宽空格、\x00 等）
        text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b\u200c\u200d\ufeff]', '', text)
        # 标准化换行：连续多个换行替换为两个换行（保留段落结构）
        text = re.sub(r'\n\s*\n', '\n\n', text)
        # 处理项目符号：确保 ● 前后空格统一
        text = re.sub(r'\s*●\s*', ' ● ', text)
        # 修复中英文之间多余空格（保留正常空格）中文后跟英文单词：在中文和字母之间加一个空格（若没有）
        text = re.sub(r'([\u4e00-\u9fa5])([a-zA-Z])', r'\1 \2', text)
        # 英文单词后跟中文：
        text = re.sub(r'([a-zA-Z])([\u4e00-\u9fa5])', r'\1 \2', text)
        # 标准化邮箱和手机号（去除内部空格，保留可点击格式）手机号：连续11位数字且以1开头，去除其中可能存在的空格或短横
        text = re.sub(r'1\s*[3-9]\s*\d\s*\d\s*\d\s*\d\s*\d\s*\d\s*\d\s*\d\s*\d', 
                      lambda m: re.sub(r'\s+', '', m.group()), text)
        # 邮箱：去除内部空格
        text = re.sub(r'([a-zA-Z0-9._%+-]+)\s*@\s*([a-zA-Z0-9.-]+)\s*\.\s*([a-zA-Z]{2,})',
                      lambda m: f"{m.group(1)}@{m.group(2)}.{m.group(3)}", text)
        # 移除连续重复的数字列表
        # text = re.sub(r'(\d+\.\s*){3,}', '', text)
        # 确保每行开头没有多余空格
        lines = text.split('\n')
        lines = [line.strip() for line in lines]
        text = '\n'.join(lines)
         # 去除首尾空白
        text = text.strip()
        return text