"""Normalize values and compare semantic equality deterministically.

Model output can vary in formatting even with deterministic sampling. Normalize
numbers, whitespace, units, null markers, and unordered paths before comparing
values used for replay keys or regression checks.
"""

import hashlib
import json
import re
import unicodedata
from decimal import Decimal, InvalidOperation

# Include the Korean null marker and an empty string as equivalent values.
NULLISH = {"", "-", "--", "n/a", "na", "none", "null", "없음", "해당없음", "미정"}

# Normalize common Korean developer terms to one lowercase English token.
# Keep the mapping in code so additions remain part of the tool and index logic.
DEV_TERMS = {
    "머지": "merge", "병합": "merge",
    "롤백": "revert", "리버트": "revert", "되돌리기": "revert",
    "커밋": "commit",
    "브랜치": "branch",
    "리팩터링": "refactor", "리팩토링": "refactor",
    "저장소": "repository", "레포": "repository", "리포지토리": "repository",
    "배포": "deploy", "디플로이": "deploy",
    "풀리퀘스트": "pull-request", "풀리퀘": "pull-request",
}


def fold_dev_terms(text: str) -> str:
    """Fold domain synonyms into canonical terms, longest form first.

    Use the result only for indexing and comparison keys, never for user-facing
    text.
    """
    for surface in sorted(DEV_TERMS, key=len, reverse=True):
        if surface in text:
            text = text.replace(surface, DEV_TERMS[surface])
    return text

# Units that may follow a number, ordered from longest to shortest.
_UNITS = ("krw", "won", "usd", "원", "₩", "$", "건", "개", "일", "items", "item")

_NUM_RE = re.compile(r"^[+-]?\d+(\.\d+)?([eE][+-]?\d+)?$")

FLOAT_PLACES = 6


def fold_text(text: str) -> str:
    """Apply NFKC normalization and collapse whitespace."""
    text = unicodedata.normalize("NFKC", text)
    return re.sub(r"\s+", " ", text).strip()


def coerce_number(value):
    """Return an ``int`` or ``Decimal`` for numeric text, otherwise ``None``.

    Accept grouping commas, parenthesized negatives, currency units, and
    scientific notation. Preserve ambiguous values as strings.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return _normalize_decimal(Decimal(repr(value)))
    if isinstance(value, Decimal):
        return _normalize_decimal(value)
    if not isinstance(value, str):
        return None

    text = fold_text(value).lower()
    if not text or text in NULLISH:
        return None

    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative, text = True, text[1:-1].strip()

    for unit in _UNITS:
        if text.endswith(unit):
            text = text[: -len(unit)].strip()
        if text.startswith(unit):
            text = text[len(unit):].strip()
    text = text.replace(",", "").replace("_", "").replace(" ", "")
    if not text or not _NUM_RE.match(text):
        return None

    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    return _normalize_decimal(-number if negative else number)


def _normalize_decimal(number: Decimal):
    quantized = number.quantize(Decimal(1).scaleb(-FLOAT_PLACES))
    if quantized == quantized.to_integral_value():
        return int(quantized)
    return quantized.normalize()


def canonicalize(value, *, unordered_paths: frozenset = frozenset(),
                 _path: str = ""):
    """Reduce a value to a comparable canonical form.

    Sort lists at paths declared unordered so ordering-only differences do not
    appear as semantic changes.
    """
    if isinstance(value, bool) or value is None:
        return value

    if isinstance(value, dict):
        out = {}
        for key in sorted(value, key=lambda k: fold_text(str(k))):
            child = f"{_path}.{key}" if _path else str(key)
            out[fold_text(str(key))] = canonicalize(
                value[key], unordered_paths=unordered_paths, _path=child)
        return out

    if isinstance(value, (list, tuple)):
        child = f"{_path}[]"
        items = [canonicalize(v, unordered_paths=unordered_paths, _path=child)
                 for v in value]
        if _path in unordered_paths:
            items.sort(key=lambda v: stable_hash(v))
        return items

    number = coerce_number(value)
    if number is not None:
        return number

    if isinstance(value, str):
        folded = fold_text(value)
        return None if folded.lower() in NULLISH else folded
    return value


def _encodable(value):
    if isinstance(value, Decimal):
        return f"D:{value}"
    if isinstance(value, dict):
        return {k: _encodable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_encodable(v) for v in value]
    return value


def stable_hash(value, length: int = 12) -> str:
    """Hash a canonical value with stable output across processes and versions."""
    payload = json.dumps(_encodable(canonicalize(value)), sort_keys=True,
                         ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]


def same(left, right) -> bool:
    """Return whether two values are semantically equal."""
    return canonicalize(left) == canonicalize(right)
