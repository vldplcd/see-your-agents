"""Build the static demo page (invented data, no server): one self-contained HTML file.

    uv run python demo/build.py                        ->  demo/dist/see-your-agents.html
    uv run python demo/build.py --out _site/index.html     (GitHub Pages, .github/workflows/pages.yml)
    uv run python demo/build.py --artifact             ->  for an artifact host that adds its own skeleton

The page is the one `sya serve` assembles from the feature registry, with every asset inlined and
demo/demo-api.js in front of app.js in place of the server.
"""

import argparse
import base64
import json
import re
from pathlib import Path

from see_your_agents.app.server import asset, page
from see_your_agents.features import FEATURES
from see_your_agents.i18n import LANGS, Catalogs, folders

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "demo" / "dist" / "see-your-agents.html"

# An artifact host wraps the page in its own document skeleton (doctype, head, charset, viewport).
SKELETON = (
    "<!doctype html>",
    '<html lang="en">',
    "<head>",
    '<meta charset="utf-8">',
    '<meta name="viewport" content="width=device-width, initial-scale=1">',
    "</head>",
    "<body>",
    "</body>",
    "</html>",
)
APP_SCRIPT = '<script src="app.js"></script>'
I18N_SCRIPT = '<script src="i18n.js"></script>'
DEMO_CSS = ".demo-note { flex-basis: 100%; font-size: 12.5px; color: var(--ink-3); }\n</style>"
LOCAL_CSS = re.compile(r'<link rel="stylesheet" href="([a-z0-9/-]+\.css)">')
LOCAL_JS = re.compile(r'<script src="([a-z0-9/-]+\.js)"></script>')
FONT_URL = re.compile(r"url\((fonts/[a-z0-9-]+\.woff2)\)")


def inline_assets(html: str) -> str:
    """The artifact is a single file: the page's own stylesheets, scripts and fonts go inline."""

    def data(url: str) -> bytes:
        found = asset(FEATURES, "/" + url)
        if found is None:
            raise SystemExit(f"page asset not found: {url}")
        return found[1]

    def font(url: str) -> str:
        return f"url(data:font/woff2;base64,{base64.b64encode(data(url)).decode()})"

    html = LOCAL_CSS.sub(lambda m: f"<style>\n{data(m[1]).decode()}</style>", html)
    html = FONT_URL.sub(lambda m: font(m[1]), html)
    return LOCAL_JS.sub(lambda m: f"<script>\n{data(m[1]).decode()}</script>", html)


def catalogs_script() -> str:
    """Every language's strings, inline: web/i18n.js picks the viewer's from them (no server to ask)."""
    catalogs = Catalogs(folders(FEATURES))
    data = json.dumps({lang: catalogs.page(lang) for lang in LANGS}, ensure_ascii=False).replace("</", "<\\/")
    return f"<script>window.SYA_I18N_ALL = {data};</script>\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the static demo page.")
    parser.add_argument("--out", type=Path, default=OUT, help="where to write it")
    parser.add_argument("--artifact", action="store_true", help="no skeleton: the artifact host adds it")
    args = parser.parse_args()

    html = page(FEATURES)
    for tag in (APP_SCRIPT, I18N_SCRIPT):
        if tag not in html:
            raise SystemExit(f"index.html changed: {tag!r} not found")
    demo_api = (ROOT / "demo/demo-api.js").read_text()
    if args.artifact:
        demo_api = "window.SYA_DEMO_ARTIFACT = true;\n" + demo_api
    html = html.replace(I18N_SCRIPT, catalogs_script() + I18N_SCRIPT, 1)
    html = inline_assets(html.replace(APP_SCRIPT, f"<script>\n{demo_api}</script>\n{APP_SCRIPT}", 1))
    if args.artifact:
        for tag in SKELETON:
            if tag not in html:
                raise SystemExit(f"index.html changed: {tag!r} not found")
            html = html.replace(tag, "", 1)
        html = html.replace(
            "<title>See your agents</title>",
            '<title>See your agents</title>\n<meta name="claude:viewport-fit-sides" content="cover">',
            1,
        )
    html = html.replace("</style>", DEMO_CSS, 1)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(html.strip() + "\n")
    print(args.out)


if __name__ == "__main__":
    main()
