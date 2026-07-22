from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel


def canonical_bytes(value: BaseModel | dict[str, Any]) -> bytes:
    payload = (
        value.model_dump(mode="json", by_alias=True) if isinstance(value, BaseModel) else value
    )
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def digest(value: BaseModel | dict[str, Any], domain: str) -> str:
    return hashlib.sha256(domain.encode("ascii") + b"\n" + canonical_bytes(value)).hexdigest()
