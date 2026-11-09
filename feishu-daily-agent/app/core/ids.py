"""稳定标识与内容摘要。"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import uuid
from typing import Any


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_text(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def stable_id(*parts: Any, prefix: str = "") -> str:
    digest = sha256_text("\x1f".join(str(part) for part in parts))[:32]
    return f"{prefix}{digest}" if prefix else digest


def normalize_text(value: str) -> str:
    """问题归一化：全半角统一、去标点/空白、转小写。"""
    text = unicodedata.normalize("NFKC", value or "").strip().lower()
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[，。！？、,.!?;；:：\"'“”‘’（）()\[\]【】<>《》]", "", text)
    return text


def question_hash(value: str) -> str:
    return sha256_text(normalize_text(value))


def new_action_id() -> str:
    return uuid.uuid4().hex


def new_claim_token() -> str:
    return uuid.uuid4().hex


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
