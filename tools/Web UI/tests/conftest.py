import os
import sys

import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 테스트는 어느 컴퓨터·레포에서나 같은 결과가 나오게 항상 예시 프로필로 (settings.local.yaml 무시).
# loop.core.config 가 import 될 때 프로필 경로를 정하므로 import 전에 정해야 한다.
os.environ["WEBUI_PROFILE"] = "example"


@pytest.fixture(autouse=True)
def _records_in_tmp(tmp_path, monkeypatch):
    """학습·평가 기록(프로필 outputs/)을 테스트마다 빈 임시 폴더로 — 진짜 기록을 안 건드리고 서로 안 섞이게."""
    from loop.core import config
    monkeypatch.setattr(config, "outputs_dir", lambda: tmp_path / "outputs")
