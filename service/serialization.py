"""模型 -> JSON 可序列化字典。"""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from typing import Any


def to_json(obj: Any) -> Any:
    if is_dataclass(obj):
        return {k: to_json(v) for k, v in asdict(obj).items()}
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: to_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [to_json(v) for v in obj]
    return obj
