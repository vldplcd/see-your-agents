"""Fetch the page's fonts from Google Fonts into the package, so the page loads nothing from the internet.

    uv run python scripts/fonts.py

Writes src/see_your_agents/web/fonts/*.woff2 (variable fonts, the latin, latin-ext and cyrillic subsets),
their SIL Open Font License texts (OFL-*.txt) from the google/fonts repository, and web/fonts.css with the
@font-face rules. Run it again only to change the families, weights or subsets.
"""

import re
import urllib.request
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "src" / "see_your_agents" / "web"
# family, weight range, its folder in github.com/google/fonts
FAMILIES = [
    ("Onest", "400..600", "ofl/onest"),
    ("Unbounded", "500..700", "ofl/unbounded"),
    ("Jost", "400..800", "ofl/jost"),
    ("Sofia Sans Condensed", "400..700", "ofl/sofiasanscondensed"),
    ("Sofia Sans Extra Condensed", "800", "ofl/sofiasansextracondensed"),
]
SUBSETS = ("latin", "latin-ext", "cyrillic")  # the UI languages; Chinese falls back to system fonts
# Google answers with woff2 only to a browser it recognizes.
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0"
FACE = re.compile(r"/\* ([a-z-]+) \*/\s*@font-face \{(.*?)\}", re.S)
SRC = re.compile(r"url\((https://fonts\.gstatic\.com/[^)]+\.woff2)\)")


def get(url: str) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=30) as resp:
        return resp.read()


def slug(family: str) -> str:
    return family.lower().replace(" ", "-")


def main() -> None:
    fonts = WEB / "fonts"
    fonts.mkdir(exist_ok=True)
    rules = []
    for family, weights, folder in FAMILIES:
        query = f"family={family.replace(' ', '+')}:wght@{weights}&display=swap"
        css = get(f"https://fonts.googleapis.com/css2?{query}")
        for subset, body in FACE.findall(css.decode()):
            if subset not in SUBSETS:
                continue
            name = f"{slug(family)}-{subset}.woff2"
            (fonts / name).write_bytes(get(SRC.search(body)[1]))
            rules.append(f"/* {family}, {subset} */\n@font-face {{{SRC.sub(f'url(fonts/{name})', body)}}}\n")
        (fonts / f"OFL-{slug(family)}.txt").write_bytes(
            get(f"https://raw.githubusercontent.com/google/fonts/main/{folder}/OFL.txt")
        )
    header = "/* From Google Fonts by scripts/fonts.py. SIL Open Font License 1.1: fonts/OFL-*.txt */\n"
    (WEB / "fonts.css").write_text(header + "\n".join(rules), encoding="utf-8")
    total = sum(f.stat().st_size for f in fonts.glob("*.woff2"))
    print(f"{len(rules)} faces, {total // 1024} KB in {fonts}")


if __name__ == "__main__":
    main()
