"""엔진: 갈아 끼우는 모델. engines/<이름>/ 하나가 모델 하나.

각 엔진 패키지는 create(settings: dict) -> Engine 을 내보낸다 (모양은 engines/base.py).
"""

import importlib
from pathlib import Path

from engines.base import Engine

ENGINES_DIR = Path(__file__).resolve().parent


class EngineNotFound(Exception):
    """설정에 적힌 엔진이 없을 때. 메시지는 한국어 안내."""


def available() -> list[str]:
    return sorted(p.name for p in ENGINES_DIR.iterdir() if (p / "__init__.py").exists())


def load(name: str, settings: dict) -> Engine:
    if name not in available():
        raise EngineNotFound(f"엔진 '{name}' 을(를) 못 찾았어. 있는 엔진: {', '.join(available()) or '없음'}. "
                             "config/settings.yaml 의 engine 을 확인해 줘.")
    return importlib.import_module(f"engines.{name}").create(settings)
