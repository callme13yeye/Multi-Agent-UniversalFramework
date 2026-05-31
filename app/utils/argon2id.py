# app/argon2id.py
from argon2 import PasswordHasher, Type
from argon2.exceptions import VerificationError, InvalidHashError

_ph = PasswordHasher(
    time_cost=3,
    memory_cost=65536,
    parallelism=4,
    hash_len=32,
    salt_len=16,
    type=Type.ID
)

def hash_password(password: str) -> str:
    return _ph.hash(password)

def verify_password(password: str, password_hash: str) -> bool:
    try:
        _ph.verify(password_hash, password)
        return True
    except (VerificationError, InvalidHashError):
        return False