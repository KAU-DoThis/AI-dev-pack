import gradio as gr

from loop.core.nav import STEPS
from loop.main import build_app
from loop.core.config import action_labels


def test_app_builds_with_all_tabs():
    app = build_app()
    tab_labels = [block.label for block in app.blocks.values() if isinstance(block, gr.Tab)]
    assert tab_labels == [name for _, name in STEPS]


def test_action_labels_loaded():
    labels = action_labels()
    assert labels and len(labels) == len(set(labels))
