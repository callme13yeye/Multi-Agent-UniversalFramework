# mineru_reader.py — MinerU PDF 智能解析读取器
# 对 mineru v3.4 pipeline 后端的封装，输出结构化 Markdown 文档。
#
# 接口兼容 llama_index BaseReader.load_data(file_path) → List[Document]，
# 可直接作为 SimpleDirectoryReader 的 file_extractor 使用。

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import List, Optional, Union

from llama_index.core import Document
from llama_index.core.readers.base import BaseReader

logger = logging.getLogger(__name__)

# mineru 输出的 parse_method 子目录名（pipeline 后端）
_PARSE_METHOD = "auto"


class MinerUReader(BaseReader):
    """MinerU PDF 解析读取器 — 本地 pipeline CPU 后端。

    功能：
    - 表格 → GFM Markdown 表格
    - 公式 → LaTeX
    - 图片提取（路径引用）
    - 多栏布局 → 正确阅读顺序
    - 109 语言 OCR

    使用方式：
        reader = MinerUReader()
        docs = reader.load_data(Path("report.pdf"))
        # docs[0].text 为结构化 Markdown
    """

    def __init__(
        self,
        backend: str = "pipeline",
        parse_method: str = "auto",
        lang: str = "ch",
        formula_enable: bool = True,
        table_enable: bool = True,
        timeout_seconds: float = 300.0,
    ):
        self.backend = backend
        self.parse_method = parse_method
        self.lang = lang
        self.formula_enable = formula_enable
        self.table_enable = table_enable
        self.timeout_seconds = timeout_seconds

        # 延迟导入，避免在未安装时启动报错
        self._do_parse = None

    def _get_do_parse(self):
        """懒加载 do_parse 函数（触发一次 import）。"""
        if self._do_parse is None:
            os.environ.setdefault("MINERU_MODEL_SOURCE", "local")
            from mineru.cli.common import do_parse

            self._do_parse = do_parse
        return self._do_parse

    # ── BaseReader 接口 ────────────────────────────────────

    def load_data(
        self,
        file_path: Union[Path, str],
        metadata: bool = True,
        extra_info: Optional[dict] = None,
    ) -> List[Document]:
        """加载 PDF 文件，返回结构化 Markdown 文档。

        Args:
            file_path: PDF 文件路径
            metadata: 是否提取文件元数据
            extra_info: 额外元数据（会合并到 Document.metadata）

        Returns:
            单元素列表，Document.text 为 Markdown 内容，
            Document.metadata 包含页数、解析耗时、文件哈希等。
        """
        file_path = Path(file_path)
        start_time = time.perf_counter()

        # 读取文件字节
        pdf_bytes = file_path.read_bytes()
        stem = file_path.stem

        # 创建临时输出目录
        output_dir = tempfile.mkdtemp(prefix="mineru_")

        try:
            do_parse = self._get_do_parse()

            # 在线程池中执行（do_parse 为同步 CPU 密集型调用）
            do_parse(
                output_dir=output_dir,
                pdf_file_names=[stem],
                pdf_bytes_list=[pdf_bytes],
                p_lang_list=[self.lang],
                backend=self.backend,
                parse_method=self.parse_method,
                formula_enable=self.formula_enable,
                table_enable=self.table_enable,
                f_draw_layout_bbox=False,
                f_draw_span_bbox=False,
                f_dump_md=True,
                f_dump_middle_json=False,
                f_dump_model_output=False,
                f_dump_orig_pdf=False,
                f_dump_content_list=False,
            )

            # 读取生成的 Markdown 文件
            # 输出路径: {output_dir}/{stem}/{parse_method}/{stem}.md
            md_path = Path(output_dir) / stem / _PARSE_METHOD / f"{stem}.md"
            if md_path.exists():
                md_content = md_path.read_text(encoding="utf-8")
            else:
                logger.warning(
                    "[MinerUReader] 未找到输出文件 %s，可能 PDF 无可提取文本",
                    md_path,
                )
                md_content = ""

            elapsed = round(time.perf_counter() - start_time, 2)
            file_hash = hashlib.sha256(pdf_bytes).hexdigest()[:16]

            logger.info(
                "[MinerUReader] 解析完成: %s → %d 字符, 耗时 %.1fs",
                file_path.name,
                len(md_content),
                elapsed,
            )

            doc_metadata: dict = {
                "file_name": file_path.name,
                "file_hash": file_hash,
                "file_size": len(pdf_bytes),
                "parser": "mineru",
                "parser_backend": self.backend,
                "parse_time_seconds": elapsed,
            }
            if extra_info:
                doc_metadata.update(extra_info)

            return [Document(text=md_content, metadata=doc_metadata)]

        except ImportError as e:
            logger.warning(
                "[MinerUReader] MinerU 未安装或缺少依赖，回退 PyMuPDF: %s", e
            )
            raise
        except Exception:
            logger.exception("[MinerUReader] 解析失败: %s", file_path.name)
            raise
        finally:
            # 清理临时目录
            import shutil

            shutil.rmtree(output_dir, ignore_errors=True)

    # ── 健康检查 ──────────────────────────────────────────

    @staticmethod
    def is_available() -> bool:
        """快速检查 MinerU 是否可用（导入 + 模型目录存在）。"""
        try:
            os.environ.setdefault("MINERU_MODEL_SOURCE", "local")
            from mineru.cli.common import do_parse  # noqa: F401
            from mineru.utils.config_reader import get_local_models_dir

            models_dir = get_local_models_dir()
            if models_dir is None:
                logger.warning("[MinerUReader] 未配置 models-dir")
                return False
            return True
        except Exception as e:
            logger.debug("[MinerUReader] 不可用: %s", e)
            return False


# ── 异步包装器（适配 async 文档处理管线）────────────────

async def async_mineru_load(
    file_path: Path,
    reader: Optional[MinerUReader] = None,
    timeout_seconds: float = 300.0,
) -> List[Document]:
    """在线程池中异步执行 MinerU 解析，避免阻塞事件循环。"""
    if reader is None:
        reader = MinerUReader(timeout_seconds=timeout_seconds)
    return await asyncio.to_thread(reader.load_data, file_path)
