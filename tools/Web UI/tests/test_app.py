import gradio as gr

from loop.main import TABS, build_app
from loop.core.config import action_labels


def test_app_builds_with_all_tabs():
    app = build_app()
    tab_labels = [block.label for block in app.blocks.values() if isinstance(block, gr.Tab)]
    assert tab_labels == [name for name, _ in TABS]


def test_action_labels_loaded():
    labels = action_labels()
    assert labels and len(labels) == len(set(labels))
