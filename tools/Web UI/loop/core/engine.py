"""루프가 엔진을 쓰는 유일한 길. 설정에 적힌 엔진을 하나 꽂아 두고 모두가 같은 걸 쓴다.

루프 코드는 특정 엔진 이름을 몰라야 한다 → 여기서도 engines.load(이름) 으로만 부른다.
"""

from functools import lru_cache

import engines
from engines.base import Decision, Engine, EngineUnavailable, Health
from loop.core.config import engine_name, engine_settings

__all__ = ["Decision", "Engine", "EngineUnavailable", "Health", "get_engine", "status_markdown"]


@lru_cache
def get_engine() -> Engine:
    name = engine_name()
    return engines.load(name, engine_settings(name))


def status_markdown(engine: Engine | None = None) -> str:
    """화면 맨 위 상태줄 (SD WebUI 처럼 항상 보이게)."""
    engine = engine or get_engine()
    h: Health = engine.health()
    where = f" · `{h.where}`" if h.where else ""
    if h.state == "ready":
        return f"🟢 **{engine.name} 준비됨** · {h.detail}{where}"
    if h.state == "loading":
        return f"🟡 **{engine.name} {h.detail or '불러오는 중'}**{where}"
    return f"🔴 **{engine.name} 꺼짐**{where} · {h.detail}"
