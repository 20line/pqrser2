from avito_watcher import captcha


def test_detect_by_text_marker():
    html = "<html><body>Пожалуйста, подтвердите, что вы не робот</body></html>"
    assert captcha.detect(html, "https://www.avito.ru/moskva") is True


def test_detect_by_url_marker():
    html = "<html><body>Всё нормально</body></html>"
    assert captcha.detect(html, "https://www.avito.ru/blocked?reason=1") is True


def test_no_false_positive_on_normal_page():
    html = "<html><body><div data-marker='item'>iPhone 13</div></body></html>"
    assert captcha.detect(html, "https://www.avito.ru/moskva/telefony") is False
