from __future__ import annotations


class MockStorageAdapter:
    def __init__(self):
        self.writes: list[dict[str, object]] = []
        self.deletes: list[str] = []

    def write_bytes(self, *, destination: str, content: bytes) -> str:
        self.writes.append({"destination": destination, "content_size": len(content)})
        return f"mock://storage/{destination}"

    def delete(self, *, target: str) -> None:
        self.deletes.append(target)

