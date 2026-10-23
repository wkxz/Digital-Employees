"""文件工具：原子写入、哈希、幂等落盘。"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_json(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def write_json(path: Path, obj: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)
    return sha256_file(path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def idempotent_write(path: Path, input_hash: str, writer: Callable[[Path], None]) -> tuple[str, bool]:
    """同一 input_hash 的重复调用直接返回已有文件，不会产生“周报(1).docx”。
    返回 (文件哈希, 是否复用)。写入先写临时文件再原子重命名。"""
    meta = path.with_name(path.name + ".meta.json")
    if path.exists() and meta.exists():
        m = read_json(meta)
        if m.get("input_hash") == input_hash and m.get("sha256") == sha256_file(path):
            return m["sha256"], True
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(".tmp-" + path.name)
    writer(tmp)
    os.replace(tmp, path)
    digest = sha256_file(path)
    write_json(meta, {"input_hash": input_hash, "sha256": digest})
    return digest, False
