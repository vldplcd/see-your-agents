"""Hello: the example feature of docs/adding-a-feature.md, run by tests/app/test_feature.py; not in FEATURES.

Inside the package a feature imports the host relatively (`from ...app.feature import Feature`); this copy
lives in tests/, so it uses the full name. Everything else is what a feature in features/hello/ would hold.
"""

from pathlib import Path

from see_your_agents.app.feature import Feature

from . import hello

FEATURE = Feature(
    id="hello",
    folder=Path(__file__).parent,
    handle_get=hello.handle_get,
    handle_post=hello.handle_post,
    tools=(hello.TOOL,),
    add_cli=hello.add_cli,
    scripts=("hello.js",),
    styles=("hello.css",),
    card_extra=hello.card_fields,
    supervisor_prompt="supervisor.md",
)
