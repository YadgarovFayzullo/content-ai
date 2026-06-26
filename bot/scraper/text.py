"""HTML → текст и общие константы веб-скрейпа (браузерные заголовки, regex ссылок).

Минимальный парсер на стандартном html.parser: видимый текст без script/style.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any

# Ссылки в HTML (href="…") — для сбора статей с индекс-страницы и фид-тегов.
_HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.I)

# Браузерные заголовки: многие сайты отдают 403 на «ботские» User-Agent.
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}


class _TextExtractor(HTMLParser):
    """Минимальный HTML→текст: собирает видимый текст, пропуская script/style и
    прочий нетекстовый мусор. Блочные теги дают перенос строки, чтобы абзацы не
    слипались в одну строку."""

    _SKIP = {"script", "style", "noscript", "template", "svg", "head"}
    _BLOCK = {
        "p", "br", "div", "section", "article", "li", "tr", "header", "footer",
        "h1", "h2", "h3", "h4", "h5", "h6",
    }

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
        if tag in self._BLOCK:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        if tag in self._BLOCK:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        self._parts.append(data)

    def text(self) -> str:
        raw = "".join(self._parts)
        lines = (re.sub(r"[ \t]+", " ", ln).strip() for ln in raw.splitlines())
        return "\n".join(ln for ln in lines if ln)


def _is_url(source: str) -> bool:
    return source.startswith("http://") or source.startswith("https://")


def _readable_text(html: str) -> str:
    """HTML → читаемый текст: короткие строки (навигация, кнопки, копирайты)
    отсекаются как шум, остаётся содержательный текст абзацами."""
    parser = _TextExtractor()
    parser.feed(html)
    lines = [ln for ln in parser.text().split("\n") if len(ln) >= 30]
    return "\n".join(lines)


def _html_to_text(html: str) -> str:
    """HTML → текст без фильтра по длине строк (для коротких описаний из фида)."""
    parser = _TextExtractor()
    parser.feed(html)
    return parser.text().strip()
