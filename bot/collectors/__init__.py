from dataclasses import dataclass


@dataclass
class RawItem:
    url: str
    text: str
    title: str | None = None
    published_at: str | None = None
