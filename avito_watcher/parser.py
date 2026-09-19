"""Извлечение карточек объявлений из DOM страницы поиска Avito.

Селекторы (`item-*`) актуальны на момент реализации и требуют проверки при
изменении вёрстки сайта (см. п.14 ТЗ — «на усмотрение разработчика»).
Функция терпима к частичным изменениям разметки: при отсутствии части полей
карточка всё равно возвращается, если удалось получить хотя бы id и ссылку.
"""

from __future__ import annotations

import re
from typing import Optional

from bs4 import BeautifulSoup, Tag

from avito_watcher.models import ListingCard

ITEM_SELECTOR = '[data-marker="item"]'
TITLE_SELECTORS = ['[data-marker="item-title"]', "a[itemprop='url']", "h3 a", "a"]
PRICE_SELECTORS = ['[data-marker="item-price"]', "meta[itemprop='price']", "[itemprop='price']"]
LOCATION_SELECTORS = ['[data-marker="item-address"]', '[data-marker="item-address-georeferences"]']

LISTING_ID_URL_RE = re.compile(r"_(\d{6,})(?:[/?]|$)")

NO_RESULTS_MARKERS = [
    "по вашему запросу ничего не найдено",
    "ничего не найдено",
    "попробуйте изменить параметры поиска",
]


def _first(item: Tag, selectors: list[str]) -> Optional[Tag]:
    for sel in selectors:
        el = item.select_one(sel)
        if el is not None:
            return el
    return None


def _extract_listing_id(item: Tag, href: str) -> Optional[str]:
    for attr in ("data-item-id", "data-id", "id"):
        val = item.get(attr)
        if val and str(val).strip().isdigit():
            return str(val).strip()
    match = LISTING_ID_URL_RE.search(href or "")
    if match:
        return match.group(1)
    return None


def parse_price(el: Optional[Tag]) -> Optional[int]:
    if el is None:
        return None
    raw = el.get("content") or el.get_text(strip=True)
    digits = re.sub(r"[^\d]", "", raw or "")
    return int(digits) if digits else None


def normalize_url(href: str) -> str:
    if not href:
        return href
    if href.startswith("http"):
        return href
    return f"https://www.avito.ru{href}"


def extract_cards(html: str) -> list[ListingCard]:
    soup = BeautifulSoup(html, "html.parser")
    cards: list[ListingCard] = []
    for item in soup.select(ITEM_SELECTOR):
        title_el = _first(item, TITLE_SELECTORS)
        if title_el is None:
            continue
        href = title_el.get("href", "") if title_el.name == "a" else ""
        if not href:
            link_el = item.select_one("a[href]")
            href = link_el.get("href", "") if link_el else ""
        listing_id = _extract_listing_id(item, href)
        if not listing_id:
            continue
        title = title_el.get_text(strip=True)
        price = parse_price(_first(item, PRICE_SELECTORS))
        loc_el = _first(item, LOCATION_SELECTORS)
        location = loc_el.get_text(strip=True) if loc_el else ""
        cards.append(
            ListingCard(
                listing_id=listing_id,
                title=title,
                price=price,
                url=normalize_url(href),
                location=location,
            )
        )
    return cards


def looks_like_no_results(html: str) -> bool:
    lowered = html.lower()
    return any(marker in lowered for marker in NO_RESULTS_MARKERS)


def looks_like_layout_change(html: str) -> bool:
    """Эвристика: карточек не найдено, и это не легитимная пустая выдача."""
    if looks_like_no_results(html):
        return False
    soup = BeautifulSoup(html, "html.parser")
    return len(soup.select(ITEM_SELECTOR)) == 0


def matches_stop_words(card: ListingCard, stop_words: list[str]) -> bool:
    text = card.title.lower()
    return any(sw.strip().lower() in text for sw in stop_words if sw and sw.strip())
