# file_hash.py
import hashlib

def compute_file_hash(file_data: bytes) -> str:
    """计算文件 SHA-256 哈希"""
    return hashlib.sha256(file_data).hexdigest()
