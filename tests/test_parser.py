from avito_watcher.models import ListingCard
from avito_watcher.parser import (
    extract_cards,
    looks_like_layout_change,
    looks_like_no_results,
    matches_stop_words,
    parse_price,
)

SAMPLE_HTML = """
<html><body>
<div data-marker="item" data-item-id="1111111111">
  <a data-marker="item-title" href="/moskva/telefony/iphone_13_128gb_1111111111">iPhone 13 128GB чёрный</a>
  <meta itemprop="price" content="35000">
  <span data-marker="item-address">Москва, Тверской р-н</span>
</div>
<div data-marker="item" data-item-id="2222222222">
  <a data-marker="item-title" href="/moskva/telefony/iphone_13_na_zapchasti_2222222222">iPhone 13 на запчасти</a>
  <span data-marker="item-price">28 000 ₽</span>
  <span data-marker="item-address">Москва, Бутово</span>
</div>
</body></html>
"""

NO_ITEMS_BUT_EMPTY_RESULT_HTML = """
<html><body><div>По вашему запросу ничего не найдено</div></body></html>
"""

NO_ITEMS_LAYOUT_CHANGED_HTML = """
<html><body><div class="some-new-markup">Что-то совсем другое</div></body></html>
"""


def test_extract_cards_basic_fields():
    cards = extract_cards(SAMPLE_HTML)
    assert len(cards) == 2

    first = cards[0]
    assert first.listing_id == "1111111111"
    assert first.title == "iPhone 13 128GB чёрный"
    assert first.price == 35000
    assert first.location == "Москва, Тверской р-н"
    assert first.url.startswith("https://www.avito.ru/")

    second = cards[1]
    assert second.listing_id == "2222222222"
    assert second.price == 28000


def test_stop_words_filtering():
    cards = extract_cards(SAMPLE_HTML)
    filtered = [c for c in cards if not matches_stop_words(c, ["на запчасти", "битый"])]
    assert len(filtered) == 1
    assert filtered[0].listing_id == "1111111111"


def test_stop_words_case_insensitive_and_empty_list():
    card = ListingCard(listing_id="1", title="Муляж iPhone", price=100, url="https://x")
    assert matches_stop_words(card, ["МУЛЯЖ"]) is True
    assert matches_stop_words(card, []) is False
    assert matches_stop_words(card, [""]) is False


def test_looks_like_no_results_is_not_layout_change():
    assert looks_like_no_results(NO_ITEMS_BUT_EMPTY_RESULT_HTML) is True
    assert looks_like_layout_change(NO_ITEMS_BUT_EMPTY_RESULT_HTML) is False


def test_looks_like_layout_change_when_no_markers_and_not_empty_result():
    assert looks_like_layout_change(NO_ITEMS_LAYOUT_CHANGED_HTML) is True


def test_looks_like_layout_change_false_when_cards_present():
    assert looks_like_layout_change(SAMPLE_HTML) is False


def test_parse_price_handles_none_and_junk():
    assert parse_price(None) is None
