# app/routes/document_routes.py — 文档管理路由
# 提供知识库管理所需的后端 API：列表（分页+搜索）、删除、替换、查看文件内容
import json
import logging
import tempfile
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, UploadFile, File, BackgroundTasks, Request, HTTPException, status, Form
from fastapi.responses import Response
from sse_starlette.sse import EventSourceResponse
from typing import Optional

from app.auth import get_current_user, get_current_user_sse
from app.index_manager import index_manager
from app.pydantic_models import DocumentInfo, DocumentListResponse, DocumentReplaceResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/documents", tags=["Documents"])

ALLOWED_EXTENSIONS = {".pdf", ".xlsx", ".docx", ".md", ".html", ".txt"}

# 文件类型 → Content-Type 映射
MIME_TYPES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def format_datetime(dt) -> str:
    """将 datetime 对象格式化为 ISO 字符串"""
    if hasattr(dt, "isoformat"):
        return dt.isoformat()
    return str(dt)


@router.get("", response_model=DocumentListResponse)
async def list_documents(
    search: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
    current_user: int = Depends(get_current_user),
):
    """列出用户文档，支持按文件名模糊搜索和分页"""
    from app.pg_database import pg_db_manager

    offset = (page - 1) * page_size
    documents, total = await pg_db_manager.search_user_documents(
        user_id=current_user, search=search, limit=page_size, offset=offset,
    )

    return DocumentListResponse(
        total=total,
        page=page,
        page_size=page_size,
        documents=[
            DocumentInfo(
                id=d["id"],
                filename=d["original_filename"],
                file_type=d["file_type"],
                file_size=d["file_size"],
                status=d["status"],
                chunk_count=d.get("chunk_count"),
                created_at=format_datetime(d["created_at"]),
                updated_at=format_datetime(d["updated_at"]) if d.get("updated_at") else None,
            )
            for d in documents
        ],
    )


@router.delete("/{doc_id}")
async def delete_document(
    doc_id: int,
    current_user: int = Depends(get_current_user),
):
    """删除指定文档（同时清理 Milvus 节点和数据库记录）"""
    success = await index_manager.delete_document(user_id=current_user, doc_id=doc_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="文档不存在或无权操作",
        )
    return {"status": "success", "message": "文档已删除"}


@router.get("/{doc_id}/file")
async def view_document_file(
    doc_id: int,
    request: Request,
    current_user: int = Depends(get_current_user),
):
    """查看文件内容 — PDF/图片可浏览器内预览，其他类型触发下载"""
    from app.pg_database import pg_db_manager

    doc = await pg_db_manager.get_document(doc_id)
    if not doc or doc["user_id"] != current_user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="文档不存在或无权操作",
        )

    minio_client = getattr(request.app.state, "minio_client", None)
    if not minio_client:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="存储服务不可用",
        )

    bucket = request.app.state.minio_bucket
    tmp_path = None
    try:
        suffix = Path(doc["original_filename"]).suffix
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp_path = Path(tmp.name)
        await minio_client.fget_object(bucket, doc["object_path"], str(tmp_path))
        content = tmp_path.read_bytes()
    except Exception as e:
        logger.error("读取 MinIO 文件失败: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="文件读取失败",
        )
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)

    file_ext = Path(doc["original_filename"]).suffix.lower()
    media_type = MIME_TYPES.get(file_ext, "application/octet-stream")

    # 可预览类型（PDF/图片）inline 展示，其余强制下载
    previewable = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".txt", ".md"}
    disposition = "inline" if file_ext in previewable else "attachment"

    filename = doc["original_filename"]
    encoded_filename = quote(filename)
    ascii_fallback = f"document{file_ext}"
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": f'{disposition}; filename="{ascii_fallback}"; filename*=UTF-8\'\'{encoded_filename}',
            "Content-Length": str(len(content)),
        },
    )


@router.get("/events")
async def document_events(
    current_user: int = Depends(get_current_user_sse),
):
    """SSE 端点：文档状态变更时主动推送给前端"""
    from app.document_event_bus import document_event_bus
    from app.pg_database import pg_db_manager
    from datetime import datetime, timezone

    sub_id = document_event_bus.subscribe()
    filename_cache: dict[int, str] = {}  # doc_id → filename

    async def _get_filename(doc_id: int) -> str:
        """从缓存或 DB 获取文件名。"""
        if doc_id not in filename_cache:
            doc = await pg_db_manager.get_document(doc_id)
            if doc:
                filename_cache[doc_id] = doc.get("original_filename", "未知文件")
            else:
                filename_cache[doc_id] = "未知文件"
        return filename_cache[doc_id]

    async def event_generator():
        try:
            async for event in document_event_bus.events(sub_id):
                if event.get("type") == "heartbeat":
                    yield {"event": "heartbeat", "data": ""}
                elif event.get("user_id") == current_user:
                    doc_id = event["doc_id"]
                    filename = await _get_filename(doc_id)
                    yield {
                        "event": "status_change",
                        "data": json.dumps({
                            "doc_id": doc_id,
                            "status": event["status"],
                            "filename": filename,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        }),
                    }
        finally:
            document_event_bus.unsubscribe(sub_id)

    return EventSourceResponse(event_generator())


@router.put("/{doc_id}/replace", response_model=DocumentReplaceResponse)
async def replace_document(
    doc_id: int,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    current_user: int = Depends(get_current_user),
    request: Request = None,
    parser_strategy: Optional[str] = Form(None),
):
    """替换文档：删除旧文档 → 上传新文件 → 后台重建索引"""
    # 校验文件类型
    file_ext = Path(file.filename).suffix.lower()
    if file_ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"不支持的文件类型，仅支持: {', '.join(ALLOWED_EXTENSIONS)}",
        )

    # 1. 删除旧文档
    success = await index_manager.delete_document(user_id=current_user, doc_id=doc_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="原文档不存在或无权操作",
        )

    # 2. 上传新文件
    data = await file.read()
    minio_client = getattr(request.app.state, "minio_client", None)
    if minio_client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="存储服务不可用",
        )

    embed_model = request.app.state.knowledge_resources["embed_model"]
    llm = request.app.state.knowledge_resources["llama_chat_llm"]

    try:
        result = await index_manager.prepare_upload(
            user_id=current_user,
            file_data=data,
            original_filename=file.filename,
            minio_client=minio_client,
            parser_strategy=parser_strategy,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(e),
        )

    # 3. 后台索引
    background_tasks.add_task(
        index_manager.process_and_index,
        user_id=current_user,
        doc_id=result["doc_id"],
        original_filename=file.filename,
        minio_client=minio_client,
        embed_model=embed_model,
        llm=llm,
        parser_strategy=parser_strategy,
    )

    return DocumentReplaceResponse(
        status="processing",
        message="文件已接收，正在替换并重建索引……",
        new_doc_id=result["doc_id"],
    )
