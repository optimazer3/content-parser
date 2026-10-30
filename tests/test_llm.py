import json
from types import SimpleNamespace

import httpx
import openai
import pytest

from bot.db import Item
from bot.llm import LLM, fallback_digest, format_materials, parse_json


def item(id, score=7, topic="t", text="текст поста"):
    return Item(id, 1, f"https://x.ru/{id}", f"Заголовок {id}", text, score, f"суть {id}", topic, "Канал")


class FakeCompletions:
    def __init__(self, replies, reject_json=False):
        self.replies = list(replies)
        self.reject_json = reject_json
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.reject_json and "response_format" in kwargs:
            response = httpx.Response(400, request=httpx.Request("POST", "https://polza.ai/api/v1/chat/completions"))
            raise openai.BadRequestError("response_format is not supported", response=response, body=None)
        content = self.replies.pop(0)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def make_llm(replies, **kw):
    completions = FakeCompletions(replies, **kw)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return LLM("key", "https://polza.ai/api/v1", "fast", "smart", client=client), completions


@pytest.mark.parametrize("raw", [
    '{"items": []}',
    '```json\n{"items": []}\n```',
    'Вот результат:\n{"items": []}\nГотово!',
])
def test_parse_json_variants(raw):
    assert parse_json(raw) == {"items": []}


def test_parse_json_array_and_garbage():
    assert parse_json('ответ: [1, 2]') == [1, 2]
    with pytest.raises(ValueError):
        parse_json("не json")


async def test_score_batch_validates_and_clamps():
    reply = json.dumps({"items": [
        {"id": 1, "score": 15, "title": "А", "summary": "с", "topic_key": "Key-A"},
        {"id": "2", "score": "4.6", "title": "Б", "summary": "с", "topic_key": "b"},
        {"id": 99, "score": 9},           # чужой id
        {"id": 3, "score": "много"},      # мусор
    ]})
    llm, completions = make_llm([reply])
    result = await llm.score_batch([item(1), item(2), item(3)], "интересы", ["Неинтересная тема"])
    assert result[1]["score"] == 10 and result[1]["topic_key"] == "key-a"
    assert result[2]["score"] == 5
    assert set(result) == {1, 2}
    call = completions.calls[0]
    assert call["model"] == "fast" and call["response_format"] == {"type": "json_object"}
    assert "Неинтересная тема" in call["messages"][0]["content"]


async def test_json_mode_falls_back_when_model_rejects_it():
    llm, completions = make_llm(['{"items": [{"id": 1, "score": 7}]}'], reject_json=True)
    assert (await llm.score_batch([item(1)], "", []))[1]["score"] == 7
    assert "response_format" not in completions.calls[-1]
    assert llm._json_mode is False


async def test_select_digest_drops_unknown_and_duplicate_ids():
    reply = json.dumps({"topics": [
        {"title": "Тема 1", "summary": "s", "item_ids": [1, 2, 42]},
        {"title": "Тема 2", "summary": "s", "item_ids": [2]},   # id 2 уже занят
        {"title": "Тема 3", "summary": "s", "item_ids": [3]},
    ]})
    llm, _ = make_llm([reply])
    topics = await llm.select_digest([item(1), item(2), item(3)], 5, "", ["Старая тема"])
    assert [(t["title"], t["item_ids"]) for t in topics] == [("Тема 1", [1, 2]), ("Тема 3", [3])]


async def test_writer_uses_smart_model_and_style():
    llm, completions = make_llm(["Готовый пост"])
    assert await llm.write_post("Тема", [item(1)], "мой стиль", "покороче") == "Готовый пост"
    call = completions.calls[0]
    assert call["model"] == "smart"
    assert "мой стиль" in call["messages"][0]["content"]
    assert "покороче" in call["messages"][1]["content"] and "https://x.ru/1" in call["messages"][1]["content"]


def test_fallback_digest_groups_by_topic_key():
    picks = fallback_digest([item(1, 9, "a"), item(2, 6, "b"), item(3, 8, "a")], 5)
    assert [p["item_ids"] for p in picks] == [[1, 3], [2]]


def test_format_materials_respects_limit():
    text = format_materials([item(n, text="х" * 10000) for n in range(1, 6)], limit=5000)
    assert len(text) <= 5000 and "[1]" in text
