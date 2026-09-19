"""Эвристики обнаружения капчи/блокировки Avito (п.8, п.11 ТЗ).

Точные признаки капчи на Avito не фиксированы в ТЗ («на усмотрение
разработчика») и могут меняться — набор маркеров сделан широким и
дополняемым.
"""

from __future__ import annotations

CAPTCHA_TEXT_MARKERS = [
    "подтвердите, что вы не робот",
    "я не робот",
    "нажмите и удерживайте",
    "проверка браузера",
    "доступ ограничен",
    "access denied",
    "recaptcha",
    "captcha",
    "необычная активность",
    "подозрительная активность",
]

CAPTCHA_URL_MARKERS = [
    "captcha",
    "blocked",
    "showcaptcha",
]


def detect(html: str, url: str) -> bool:
    lowered_html = (html or "").lower()
    if any(marker in lowered_html for marker in CAPTCHA_TEXT_MARKERS):
        return True
    lowered_url = (url or "").lower()
    if any(marker in lowered_url for marker in CAPTCHA_URL_MARKERS):
        return True
    return False
