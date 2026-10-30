import pytest

from bot.config import Settings, parse_times, system_proxy, telethon_proxy
from bot.sources import classify_ref, split_refs
from bot.text import sanitize_html, split_message, strip_html, text_hash


@pytest.mark.parametrize("raw, kind, value", [
    ("@Neural_News", "tg", "neural_news"),
    ("https://t.me/neural_news", "tg", "neural_news"),
    ("t.me/s/neural_news/", "tg", "neural_news"),
    ("https://t.me/neural_news/1234", "tg", "neural_news"),
    ("https://telegram.me/neural_news", "tg", "neural_news"),
    ("https://t.me/c/1987654321/55", "tg", "id:-1001987654321"),
    ("https://t.me/+AbCdEf123", "tg_invite", "https://t.me/+AbCdEf123"),
    ("https://t.me/joinchat/AbCdEf", "tg_invite", "https://t.me/joinchat/AbCdEf"),
    ("neural_news", "tg", "neural_news"),
    ("habr.com/ru/hubs/artificial_intelligence/", "web", "https://habr.com/ru/hubs/artificial_intelligence/"),
    ("https://www.opennet.ru", "web", "https://www.opennet.ru/"),
    ("https://site.ru/news", "web", "https://site.ru/news"),
    ("@ab", "bad", "@ab"),
    ("что-то", "bad", "что-то"),
])
def test_classify_ref(raw, kind, value):
    ref = classify_ref(raw)
    assert (ref.kind, ref.value) == (kind, value)


def test_split_refs():
    assert split_refs("@a1234, https://x.ru\n@b5678") == ["@a1234", "https://x.ru", "@b5678"]


def test_text_hash_ignores_links_and_punctuation():
    a = "Вышла GPT-6!  Подробности: https://t.me/a/1"
    b = "вышла gpt 6 подробности https://example.com/x"
    assert text_hash(a) == text_hash(b)
    assert text_hash(a) != text_hash("Вышла GPT-7")


def test_strip_html():
    assert strip_html("<p>Привет&nbsp;<b>мир</b></p><p>Второй</p>") == "Привет мир\nВторой"


def test_sanitize_html_keeps_telegram_tags_and_escapes_rest():
    raw = '<b>Жирный</b> <span>span</span> 2 < 3 <GPT> <a href="https://x.ru/?a=1&b=2" target="_blank">ссылка</a>'
    assert sanitize_html(raw) == ('<b>Жирный</b> span 2 &lt; 3 &lt;GPT&gt; '
                                  '<a href="https://x.ru/?a=1&amp;b=2">ссылка</a>')


def test_sanitize_html_flattens_ordinary_html():
    raw = "<p>Первый абзац<br>строка</p><p>Второй</p><ul><li>раз</li><li>два</li></ul>"
    assert sanitize_html(raw) == "Первый абзац\nстрока\n\nВторой\n\n• раз\n• два"


def test_sanitize_html_converts_markdown_and_closes_tags():
    assert sanitize_html("## Заголовок\n**важно** и <i>курсив") == "<b>Заголовок</b>\n<b>важно</b> и <i>курсив</i>"


def test_sanitize_html_fixes_crossed_tags():
    assert sanitize_html("<b>a <i>b</b> c</i>") == "<b>a <i>b</i></b> c"


def test_sanitize_keeps_existing_entities_single_escaped():
    assert sanitize_html("AT&amp;T &lt;3 &quot;x&quot;") == 'AT&amp;T &lt;3 "x"'


def test_split_message():
    text = "\n\n".join(["а" * 3000, "б" * 3000, "в" * 100])
    parts = split_message(text)
    assert [len(p) for p in parts] == [3000, 3102]
    assert all(len(p) <= 4096 for p in split_message("г" * 10000))
    assert split_message("коротко") == ["коротко"]


def test_parse_times():
    assert parse_times("19:30, 9:00") == [(9, 0), (19, 30)]
    assert parse_times("10 18") == [(10, 0), (18, 0)]
    with pytest.raises(ValueError):
        parse_times("25:00")
    with pytest.raises(ValueError):
        parse_times("")


def test_telethon_proxy():
    assert telethon_proxy("") is None
    assert telethon_proxy("socks5://u:p@127.0.0.1:1080") == {
        "proxy_type": "socks5", "addr": "127.0.0.1", "port": 1080, "username": "u", "password": "p", "rdns": True,
    }


@pytest.mark.parametrize("proxies, expected", [
    ({}, ""),
    # так Windows отдаёт прокси VPN-клиента в режиме «системный прокси»
    ({"http": "http://127.0.0.1:10809", "https": "https://127.0.0.1:10809"}, "http://127.0.0.1:10809"),
    ({"socks": "socks://127.0.0.1:10808", "http": "http://127.0.0.1:10809"}, "socks5://127.0.0.1:10808"),
    ({"https": "127.0.0.1:7890"}, "http://127.0.0.1:7890"),
    ({"no": "localhost"}, ""),
])
def test_system_proxy(monkeypatch, proxies, expected):
    monkeypatch.setattr("urllib.request.getproxies", lambda: proxies)
    assert system_proxy() == expected


def test_proxy_url_modes(monkeypatch):
    monkeypatch.setattr("urllib.request.getproxies", lambda: {"http": "http://127.0.0.1:10809"})
    assert Settings(_env_file=None).proxy_url == "http://127.0.0.1:10809"  # по умолчанию auto
    assert Settings(_env_file=None, tg_proxy="off").proxy_url == ""
    assert Settings(_env_file=None, tg_proxy="socks5://1.2.3.4:1080").proxy_url == "socks5://1.2.3.4:1080"
