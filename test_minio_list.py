"""
MinIO 文件查询脚本
查看 MinIO 中有哪些用户上传了文件，以及文件内容。

用法:
    uv run python test_minio_list.py          # 仅列出文件
    uv run python test_minio_list.py --all    # 列出文件并尝试读取内容
"""
import argparse
import asyncio
import os
import sys
from collections import defaultdict

from miniopy_async import Minio


def load_env(path: str = "key.env") -> dict:
    env = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"')
    return env


def format_size(size: int) -> str:
    if size > 1024 * 1024:
        return f"{size / 1024 / 1024:.1f} MB"
    elif size > 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size} B"


TEXT_EXTENSIONS = {
    ".txt", ".md", ".html", ".json", ".yaml", ".yml",
    ".xml", ".csv", ".log", ".py", ".js", ".sh",
    ".ini", ".cfg", ".conf", ".toml",
}
MAX_PREVIEW_SIZE = 100 * 1024  # 100KB


async def list_all(client: Minio, bucket: str) -> list:
    """列出 bucket 中所有对象"""
    objects = []
    async for obj in client.list_objects(bucket, recursive=True):
        objects.append(obj)
    return objects


def group_by_user(objects: list) -> dict[str, list]:
    """按 user_id 分组"""
    groups = defaultdict(list)
    for obj in objects:
        parts = obj.object_name.split("/", 1)
        user_id = parts[0]
        filename = parts[1] if len(parts) > 1 else obj.object_name
        groups[user_id].append((filename, obj))
    return dict(groups)


def print_file_list(user_files: dict[str, list]):
    """打印文件列表"""
    print("=" * 70)
    print(f"[用户文件列表] 共 {len(user_files)} 个用户")
    print("=" * 70)

    for uid in sorted(user_files.keys()):
        files = user_files[uid]
        print(f"\n[用户 ID: {uid}]  {len(files)} 个文件")
        print(f"   {'文件名':<40} {'大小':<12} {'类型':<10} {'上次修改'}")
        print(f"   {'-' * 40} {'-' * 12} {'-' * 10} {'-' * 20}")
        for fname, obj in files:
            last_modified = (
                obj.last_modified.strftime("%Y-%m-%d %H:%M")
                if obj.last_modified
                else "未知"
            )
            print(
                f"   {fname:<40} "
                f"{format_size(obj.size):<12} "
                f"{(obj.content_type or '未知'):<10} "
                f"{last_modified}"
            )


async def preview_file(client: Minio, bucket: str, object_name: str):
    """读取并打印文本文件内容（前 80 行）"""
    response = None
    try:
        response = await client.get_object(bucket, object_name)
        raw = await response.read()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("utf-8", errors="replace")

        lines = text.splitlines()
        for line in lines[:80]:
            print(line)
        if len(lines) > 80:
            print(f"... (共 {len(lines)} 行，仅显示前 80 行)")
    except Exception as e:
        print(f"[ERROR] 读取失败: {e}")
    finally:
        if response:
            await response.close()


async def preview_all(user_files: dict[str, list], client: Minio, bucket: str):
    """预览所有用户的文本文件内容"""
    print("\n" + "=" * 70)
    print("[文件内容预览]")
    print("=" * 70)

    for uid in sorted(user_files.keys()):
        files = user_files[uid]
        for fname, obj in files:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in TEXT_EXTENSIONS:
                print(f"\n[跳过非文本] {uid}/{fname} (类型: {ext or obj.content_type or '未知'})")
                continue
            if obj.size > MAX_PREVIEW_SIZE:
                print(f"\n[跳过大文件] {uid}/{fname} ({format_size(obj.size)})")
                continue

            print(f"\n[{uid}/{fname}] 大小: {format_size(obj.size)}")
            print("-" * 60)
            await preview_file(client, bucket, obj.object_name)


async def main():
    parser = argparse.ArgumentParser(description="MinIO 文件查询工具")
    parser.add_argument("--all", action="store_true", help="同时预览文本文件内容")
    args = parser.parse_args()

    env = load_env()
    endpoint = env.get("MINIO_ENDPOINT", "localhost:9002")
    access_key = env.get("MINIO_ACCESS_KEY", "minioadmin")
    secret_key = env.get("MINIO_SECRET_KEY", "minioadmin")
    secure = env.get("MINIO_SECURE", "False").lower() == "true"
    bucket = env.get("MINIO_BUCKET_NAME", "user-documents")

    client = Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=secure)

    # 检查 bucket
    if not await client.bucket_exists(bucket):
        print(f"[ERROR] Bucket '{bucket}' 不存在!")
        sys.exit(1)

    # 列出所有文件
    objects = await list_all(client, bucket)
    if not objects:
        print(f"Bucket '{bucket}' 中没有文件。")
        return

    print(f"Bucket: {bucket}")
    print(f"文件总数: {len(objects)}\n")

    user_files = group_by_user(objects)
    print_file_list(user_files)

    if args.all:
        await preview_all(user_files, client, bucket)

    print("\n[完成] 查询结束")


if __name__ == "__main__":
    asyncio.run(main())
