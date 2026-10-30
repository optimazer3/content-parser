"""Заглушка нейросети для тестов и пробного прогона без ключа Polza."""

from .db import Item
from .llm import fallback_digest
from .text import normalize, shorten

KEYWORDS = ("нейросет", "ии", "ai", "gpt", "llm", "модел", "openai", "claude", "gemini", "искусствен")


class FakeLLM:
    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []

    async def score_batch(self, items: list[Item], interests: str, disliked: list[str]) -> dict[int, dict]:
        self.calls.append(("score_batch", tuple(i.id for i in items)))
        result = {}
        for item in items:
            words = normalize(item.text).split()
            hits = sum(1 for w in words if w.startswith(KEYWORDS))
            first_line = next((line for line in item.text.splitlines() if line.strip()), item.text)
            result[item.id] = {
                "score": min(10, 3 + hits),
                "title": shorten(item.title or first_line, 90),
                "summary": shorten(item.text.replace("\n", " "), 160),
                "topic_key": "-".join(words[:3]),
            }
        return result

    async def select_digest(self, candidates: list[Item], size: int, interests: str, sent: list[str]) -> list[dict]:
        self.calls.append(("select_digest", tuple(i.id for i in candidates)))
        return fallback_digest(candidates, size)

    async def rewrite(self, text: str, style: str, instruction: str | None = None) -> str:
        self.calls.append(("rewrite", (instruction,)))
        return f"<b>[уникализация]</b>\n{shorten(text, 300)}"

    async def write_post(self, topic: str, materials: list[Item], style: str, instruction: str | None = None) -> str:
        self.calls.append(("write_post", (topic, len(materials), instruction)))
        return f"<b>[пост] {topic}</b>\nМатериалов: {len(materials)}"

    async def explain(self, topic: str, materials: list[Item]) -> str:
        self.calls.append(("explain", (topic,)))
        return f"[подробнее] {topic}"

    async def refine(self, text: str, instruction: str, style: str, materials: list[Item]) -> str:
        self.calls.append(("refine", (instruction,)))
        return f"{text}\n[правка: {instruction}]"
