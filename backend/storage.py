"""文件存储路径模型与路径安全校验。

目录约定（规格书 1.1.8）：
    uploads/{user_id}/{conversation_id}/    附件
    exports/{user_id}/{conversation_id}/    导出结果
    workspace/{user_id}/{conversation_id}/  临时中间文件

删除会话时三个目录一并删除。
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from backend import config

KINDS = ("uploads", "exports", "workspace")

# Windows 非法文件名字符 + 控制字符
_ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


# ------------------------------------------------------------------ 基础路径


def data_dir() -> Path:
    return config.data_dir()


def logs_dir() -> Path:
    return data_dir() / "logs"


def secret_key_path() -> Path:
    return data_dir() / "secret.key"


def kind_dir(kind: str) -> Path:
    if kind not in KINDS:
        raise ValueError(f"未知的存储类别: {kind}")
    return data_dir() / kind


def user_dir(kind: str, user_id: int) -> Path:
    return kind_dir(kind) / str(int(user_id))


def conv_dir(kind: str, user_id: int, conversation_id: int) -> Path:
    return user_dir(kind, user_id) / str(int(conversation_id))


def upload_dir(user_id: int, conversation_id: int) -> Path:
    return conv_dir("uploads", user_id, conversation_id)


def export_dir(user_id: int, conversation_id: int) -> Path:
    return conv_dir("exports", user_id, conversation_id)


def workspace_dir(user_id: int, conversation_id: int) -> Path:
    return conv_dir("workspace", user_id, conversation_id)


def ensure_conv_dirs(user_id: int, conversation_id: int) -> dict[str, Path]:
    """确保会话三个目录存在，返回路径字典。"""
    dirs = {
        "uploads": upload_dir(user_id, conversation_id),
        "exports": export_dir(user_id, conversation_id),
        "workspace": workspace_dir(user_id, conversation_id),
    }
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    return dirs


def ensure_all() -> None:
    """启动时创建数据根目录。"""
    data_dir().mkdir(parents=True, exist_ok=True)
    logs_dir().mkdir(parents=True, exist_ok=True)
    for kind in KINDS:
        kind_dir(kind).mkdir(parents=True, exist_ok=True)


def delete_conv_dirs(user_id: int, conversation_id: int) -> list[str]:
    """删除会话对应的三个目录，返回被删除的目录列表。"""
    removed: list[str] = []
    for kind in KINDS:
        path = conv_dir(kind, user_id, conversation_id)
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
            removed.append(str(path))
        # 顺带清理空的用户目录
        parent = path.parent
        if parent.exists() and not any(parent.iterdir()):
            try:
                parent.rmdir()
            except OSError:
                pass
    return removed


# ------------------------------------------------------------------ 安全校验


def is_within(path: str | Path, base: str | Path) -> bool:
    """path 是否位于 base 目录之内（含相等）。"""
    try:
        Path(path).resolve().relative_to(Path(base).resolve())
        return True
    except (ValueError, OSError):
        return False


def safe_join(base: str | Path, *parts: str) -> Path:
    """在 base 下安全拼接路径，越界时抛 ValueError。"""
    base_path = Path(base).resolve()
    candidate = base_path.joinpath(*[str(p) for p in parts]).resolve()
    if not is_within(candidate, base_path):
        raise ValueError(f"路径越界: {'/'.join(str(p) for p in parts)}")
    return candidate


def sanitize_filename(name: str, fallback: str = "file") -> str:
    """清洗文件名：去掉目录部分与非法字符，保留中文。

    Windows 文件系统大小写不敏感且禁止结尾的点/空格，一并处理。
    """
    raw = (name or "").strip()
    raw = raw.replace("\\", "/").split("/")[-1]  # 丢弃任何目录部分
    cleaned = _ILLEGAL_CHARS.sub("_", raw).strip(" .")
    if cleaned in ("", ".", ".."):
        return fallback
    if len(cleaned) > 120:
        suffix = Path(cleaned).suffix[:20]
        cleaned = Path(cleaned).stem[:100] + suffix
    return cleaned


def file_type_of(filename: str) -> str:
    """由扩展名推导文件类型（不含点，小写）。"""
    return Path(filename).suffix.lstrip(".").lower() or "unknown"


def human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"
