"""Нейросеть через Polza.ai (OpenAI-совместимый API)."""

import json
import logging
import re

import openai
from openai import AsyncOpenAI

from . import prompts
from .db import Item
from .text import shorten

log = logging.getLogger(__name__)

MATERIALS_LIMIT = 14000  # символов материалов в одном запросе на написание


def parse_json(text: str):
    """Достаёт JSON из ответа модели, даже если она обернула его в ```json или добавила слова."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start, end = text.find(open_ch), text.rfind(close_ch)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError(f"в ответе модели нет JSON: {shorten(text, 200)!r}")


def _as_list(data, key: str) -> list:
    if isinstance(data, dict):
        data = data.get(key, [])
    return data if isinstance(data, list) else []


def format_materials(items: list[Item], limit: int = MATERIALS_LIMIT) -> str:
    if not items:
        return ""
    per_item = max(1500, limit // len(items))
    blocks = []
    for n, item in enumerate(items, 1):
        head = f"[{n}] {item.source_name or 'Источник'} — {item.url}"
        if item.title:
            head += f"\n{item.title}"
        blocks.append(f"{head}\n{shorten(item.text, per_item)}")
    return shorten("\n\n".join(blocks), limit)


def fallback_digest(candidates: list[Item], size: int) -> list[dict]:
    """Если модель не справилась с подборкой: группируем по topic_key и берём лучшие (size=0 — все)."""
    groups: dict[str, list[Item]] = {}
    for item in candidates:
        groups.setdefault(item.topic_key or f"item-{item.id}", []).append(item)
    ranked = sorted(groups.values(), key=lambda g: max(i.score or 0 for i in g), reverse=True)
    return [
        {"title": g[0].title or shorten(g[0].text, 80), "summary": g[0].summary or "", "item_ids": [i.id for i in g]}
        for g in ranked[:size or None]
    ]


class LLM:
    def __init__(self, api_key: str, base_url: str, fast_model: str, smart_model: str, client=None):
        self.client = client or AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=180, max_retries=2)
        self.fast_model = fast_model
        self.smart_model = smart_model
        self._json_mode = True

    async def chat(self, model: str, system: str, user: str, json_mode: bool = False) -> str:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        kwargs = {"response_format": {"type": "json_object"}} if json_mode and self._json_mode else {}
        try:
            resp = await self.client.chat.completions.create(model=model, messages=messages, **kwargs)
        except openai.BadRequestError:
            if not kwargs:
                raise
            # не все модели у агрегатора понимают response_format — просим JSON словами
            log.info("Модель %s не поддерживает response_format, работаем без него", model)
            self._json_mode = False
            resp = await self.client.chat.completions.create(model=model, messages=messages)
        return (resp.choices[0].message.content or "").strip()

    # --- отбор ---

    async def score_batch(self, items: list[Item], interests: str, disliked: list[str]) -> dict[int, dict]:
        dislikes = prompts.DISLIKES.format(titles="\n".join(f"- {t}" for t in disliked)) if disliked else ""
        system = prompts.SCORE_SYSTEM.format(interests=interests, dislikes=dislikes)
        user = "\n\n".join(
            f"id={i.id} | источник: {i.source_name}\n{('Заголовок: ' + i.title + chr(10)) if i.title else ''}"
            f"{shorten(i.text, 1200)}"
            for i in items
        )
        data = parse_json(await self.chat(self.fast_model, system, user, json_mode=True))
        ids = {i.id for i in items}
        result = {}
        for row in _as_list(data, "items"):
            try:
                item_id = int(row["id"])
                score = max(0, min(10, int(round(float(row.get("score", 0))))))
            except (KeyError, TypeError, ValueError):
                continue
            if str(row.get("relevant", True)).lower() in ("false", "0", "no"):
                score = min(score, 1)  # не по тематике — в подборку не попадёт, какой бы важной ни была новость
            if item_id in ids:
                result[item_id] = {
                    "score": score,
                    "title": shorten(str(row.get("title") or ""), 120),
                    "summary": str(row.get("summary") or ""),
                    "topic_key": str(row.get("topic_key") or "").lower(),
                }
        return result

    async def select_digest(self, candidates: list[Item], size: int, interests: str, sent: list[str]) -> list[dict]:
        """size=0 — все темы по тематике, без ограничения."""
        system = prompts.SELECT_SYSTEM.format(
            interests=interests, sent="\n".join(f"- {t}" for t in sent) or "- (ничего)",
            limit=prompts.SELECT_LIMIT.format(size=size) if size else prompts.SELECT_ALL,
        )
        user = "\n".join(
            f"id={i.id} | score={i.score} | {i.source_name} | {i.title} — {i.summary} [{i.topic_key}]"
            for i in candidates
        )
        data = parse_json(await self.chat(self.fast_model, system, user, json_mode=True))
        by_id = {i.id for i in candidates}
        used: set[int] = set()
        topics = []
        for row in _as_list(data, "topics"):
            if not isinstance(row, dict):
                continue
            ids = []
            for raw in row.get("item_ids") or []:
                try:
                    item_id = int(raw)
                except (TypeError, ValueError):
                    continue
                if item_id in by_id and item_id not in used:
                    ids.append(item_id)
                    used.add(item_id)
            title = str(row.get("title") or "").strip()
            if ids and title:
                topics.append({"title": shorten(title, 120), "summary": str(row.get("summary") or ""), "item_ids": ids})
        return topics[:size or None]

    # --- написание ---

    def _writer(self, style: str, skill: str = "") -> str:
        return prompts.WRITER_SYSTEM.format(
            skill=prompts.SKILL.format(skill=skill) if skill else "",
            style=prompts.STYLE.format(style=style) if style else "",
        )

    @staticmethod
    def _instruction(instruction: str | None) -> str:
        return f"\nДополнительное указание: {instruction}\n" if instruction else ""

    async def rewrite(self, text: str, style: str, instruction: str | None = None, skill: str = "") -> str:
        task = prompts.REWRITE_TASK.format(text=shorten(text, MATERIALS_LIMIT), instruction=self._instruction(instruction))
        return await self.chat(self.smart_model, self._writer(style, skill), task)

    async def write_post(self, topic: str, materials: list[Item], style: str, instruction: str | None = None,
                         skill: str = "") -> str:
        template = prompts.WRITE_TASK if materials else prompts.WRITE_NO_MATERIALS
        task = template.format(topic=topic, materials=format_materials(materials), instruction=self._instruction(instruction))
        return await self.chat(self.smart_model, self._writer(style, skill), task)

    async def explain(self, topic: str, materials: list[Item]) -> str:
        task = prompts.EXPLAIN_TASK.format(topic=topic, materials=format_materials(materials))
        return await self.chat(self.smart_model, self._writer(""), task)

    async def refine(self, text: str, instruction: str, style: str, materials: list[Item], skill: str = "") -> str:
        extra = prompts.REFINE_MATERIALS.format(materials=format_materials(materials, 8000)) if materials else ""
        task = prompts.REFINE_TASK.format(text=text, instruction=instruction, materials=extra)
        return await self.chat(self.smart_model, self._writer(style, skill), task)
