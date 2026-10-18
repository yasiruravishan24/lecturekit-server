"""Shared request checks."""
import unicodedata
from typing import Annotated

from fastapi import Path

# ids in the URL: a positive number that fits the database (a bigger one would crash SQLite instead of giving 404)
Id = Annotated[int, Path(ge=1, le=2_147_483_647)]


def clean_text(value: str) -> str:
    """Trim and collapse spaces; refuse control characters (newlines, NUL, ...) which have no place in a name or title."""
    value = value.strip()
    if any(unicodedata.category(ch) in ("Cc", "Cf", "Cs") for ch in value):
        raise ValueError("must not contain control characters")
    if not value:
        raise ValueError("must not be empty")
    return " ".join(value.split())
