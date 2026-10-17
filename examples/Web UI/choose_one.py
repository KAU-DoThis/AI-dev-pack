"""Web UI 엔진으로 한 건 판단해 보기 (화면 없이).

준비: Jeff 서버가 켜져 있어야 한다 (docs/Web UI.md '3. 사용 방법').
실행 (저장소 맨 위에서, 웹 UI 환경으로):
    "tools/Web UI/.venv/Scripts/python" "examples/Web UI/choose_one.py"
"""

import sys
from pathlib import Path

WEBUI = Path(__file__).resolve().parents[2] / "tools" / "Web UI"
sys.path.insert(0, str(WEBUI))

from engines import load  # noqa: E402  (위에서 경로를 넣은 뒤 import)
from engines.base import EngineUnavailable  # noqa: E402

# config/settings.yaml 의 engines.jeff 와 같은 모양
engine = load("jeff", {"url": "http://127.0.0.1:8765", "model": "", "timeout": 30})

state = {
    "company": "Northwind Shoes",
    "channel": "email",
    "message": "My card shows two payments for the same order. Please send one back.",
}
options = {
    "refund": "Refunds, double charges and money back",
    "delivery": "Shipping, tracking and delivery problems",
    "account": "Logging in, passwords and account settings",
    "other": "Not for any of these teams",
}

health = engine.health()
if health.state != "ready":
    sys.exit(f"Jeff 서버가 준비 안 됨 ({health.state}): {health.detail}")

try:
    decision = engine.choose(state, options, instructions="Which team should handle this message?")
except EngineUnavailable as error:
    sys.exit(str(error))

print(f"고른 것: {decision.choice} (확신도 {decision.confidence:.2f}, {decision.elapsed_ms:.0f}ms)")
for option, probability in decision.ranked():
    print(f"  {option:<9} {probability:.3f}")
