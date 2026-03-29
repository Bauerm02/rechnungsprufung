from __future__ import annotations

from typing import Protocol


class StoragePort(Protocol):
    def write_bytes(self, *, destination: str, content: bytes) -> str:
        ...

    def delete(self, *, target: str) -> None:
        ...

