"""Собирает web/app.html и библиотеки из web/lib в один файл index.html (работает офлайн)."""

import base64
import re
import sys
from pathlib import Path

WEB = Path(__file__).parent
OUT = WEB.parent / "index.html"


def lib_text(name):
    text = (WEB / "lib" / name).read_text(encoding="utf-8")
    low = text.lower()
    # такие последовательности сломали бы разбор встроенного <script>
    assert "</script" not in low and "<!--" not in low, name
    # литерал U+FFFD в строках JS -> равнозначный escape (иначе файл выглядит «битым»)
    return text.replace("\ufffd", "\\uFFFD")


def main():
    html = (WEB / "app.html").read_text(encoding="utf-8")

    def inline_script(m):
        return f"<script>/* {m.group(1)} */\n{lib_text(m.group(1))}\n</script>"

    def inline_data(m):
        name = m.group(2)
        if name.endswith(".js"):
            data = lib_text(name)
        else:
            data = base64.b64encode((WEB / "lib" / name).read_bytes()).decode()
        return f'<script id="{m.group(1)}" type="text/plain">{data}</script>'

    html = re.sub(r'<script src="lib/([^"]+)"></script>', inline_script, html)
    html = re.sub(r'<script id="([^"]+)" type="text/plain" data-src="lib/([^"]+)"></script>',
                  inline_data, html)
    assert 'src="lib/' not in html and 'data-src="lib/' not in html
    OUT.write_text(html, encoding="utf-8")
    print(f"{OUT} — {OUT.stat().st_size / 1e6:.1f} МБ")
    if len(sys.argv) > 2 and sys.argv[1] == "--artifact":
        write_artifact(html, Path(sys.argv[2]))


def write_artifact(html, path):
    """Вариант для публикации на claude.ai: без html/head/body, заголовок и стили в начале."""
    head = re.search(r"<head>(.*?)</head>", html, re.S).group(1)
    body = re.search(r"<body>(.*)</body>", html, re.S).group(1)
    title = re.search(r"<title>.*?</title>", head, re.S).group(0)
    style = re.search(r"<style>.*?</style>", head, re.S).group(0)
    scripts = "".join(re.findall(r"<script.*?</script>", head, re.S))
    path.write_text(f"{title}\n{style}\n{scripts}\n{body}", encoding="utf-8")
    print(f"{path} — {path.stat().st_size / 1e6:.1f} МБ")


if __name__ == "__main__":
    main()
