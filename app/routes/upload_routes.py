# app/routes/upload_routes.py — 仅负责文件上传、校验、落盘
# 文档处理（加载 → 分析 → 切分 → 元数据注入）委托给 document_processor，
# 索引生命周期管理委托给 index_manager。
# ================================================
import logging
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import APIRouter, Depends, UploadFile, File, HTTPException, status, BackgroundTasks, Request, Form

from app.auth import get_current_user
from app.documents import index_manager
from app.documents.document_status import DocumentStatus
from app.pydantic_models import UploadResponse

logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).parent.parent.parent
ENV_PATH = PROJECT_ROOT / "key.env"

load_dotenv(dotenv_path=ENV_PATH)
router = APIRouter(prefix="/upload", tags=["Upload"])

ALLOWED_EXTENSIONS = {".pdf", ".xlsx", ".docx", ".md", ".html", ".txt"}


@router.post("", response_model=UploadResponse)
async def upload_file(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    current_user: int = Depends(get_current_user),
    request: Request = None,
    parser_strategy: Optional[str] = Form(None),
    on_duplicate: str = Form("skip"),
):
    """
    用户上传文件至 MinIO，后台处理并加入知识库。

    去重策略 on_duplicate:
      - skip:    文件已索引则跳过（默认）
      - rebuild: 文件已存在则删除旧索引，重建
      - error:   文件已存在则返回 409 冲突

    """
    # 校验文件类型
    file_ext = Path(file.filename).suffix.lower()
    if file_ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"不支持的文件类型，仅支持: {', '.join(ALLOWED_EXTENSIONS)}"
        )

    # 校验 on_duplicate 参数
    if on_duplicate not in ("skip", "rebuild", "error"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="on_duplicate 必须为 skip / rebuild / error"
        )

    # 检查 MinIO 是否可用
    minio_client = getattr(request.app.state, "minio_client", None)
    if minio_client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="存储服务不可用，请联系管理员"
        )

    # 读取文件数据
    data = await file.read()

    # 获取模型实例
    embed_model = request.app.state.knowledge_resources["embed_model"]
    llm = request.app.state.knowledge_resources["llama_chat_llm"]

    # 编排：哈希 → 去重 → 上传 MinIO → 创建文档记录
    try:
        result = await index_manager.prepare_upload(
            user_id=current_user,
            file_data=data,
            original_filename=file.filename,
            minio_client=minio_client,
            parser_strategy=parser_strategy,
            on_duplicate=on_duplicate,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(e),
        )

    # 重复文件被跳过
    if result["status"] == DocumentStatus.SKIPPED:
        return UploadResponse(
            filename=file.filename,
            status=DocumentStatus.SKIPPED,
            message=result["message"],
        )

    # 后台索引
    gateway = getattr(request.app.state, "model_gateway", None)
    background_tasks.add_task(
        index_manager.process_and_index,
        user_id=current_user,
        doc_id=result["doc_id"],
        original_filename=file.filename,
        minio_client=minio_client,
        embed_model=embed_model,
        llm=llm,
        parser_strategy=parser_strategy,
        gateway=gateway,
    )

    return UploadResponse(
        filename=file.filename,
        status=DocumentStatus.QUEUING,
        message="文件已接收，正在后台处理并添加到知识库……",
    )
