"""Собирает web/app.html и библиотеки из web/lib в один файл index.html (работает офлайн)."""

import base64
import re
from pathlib import Path

WEB = Path(__file__).parent
OUT = WEB.parent / "index.html"


def lib_text(name):
    text = (WEB / "lib" / name).read_text(encoding="utf-8")
    low = text.lower()
    # такие последовательности сломали бы разбор встроенного <script>
    assert "</script" not in low and "<!--" not in low, name
    return text


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


if __name__ == "__main__":
    main()
