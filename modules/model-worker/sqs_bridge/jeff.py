"""Jeff 워커: jeff-requests 큐 → 이 PC의 jeff-serve → agent-replies 큐.

실행:  python -m sqs_bridge  (JEFF_ENABLED=true, __main__.py)
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from .messages import ModelUnavailable, call_limit


ROOT = Path(__file__).resolve().parent.parent


def load_env(path: Path = ROOT / ".env") -> None:
    """KEY=VALUE 줄만 읽음. 이미 있는 환경변수는 안 덮어씀."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def load_prompt(path: str) -> tuple[dict, str]:
    """웹 UI 프로필의 prompt.*.yaml(options·instructions) — 학습 때와 같은 문장을 쓰려고."""
    if not path:
        return {}, ""
    import yaml
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return dict(data.get("options") or {}), str(data.get("instructions") or "")


def make_handler(url: str, model: str = "", timeout: float = 30.0, options: dict | None = None,
                 instructions: str = "", post=None):
    """payload = {"state": {...}, "options"?: {id: 설명}, "instructions"?: str, "model"?: str}
    → {"choice", "probabilities", "confidence", "model"}"""
    post = post or _post
    url = url.rstrip("/")

    def handle(payload: dict, time_left: float | None = None) -> dict:
        deadline = time.monotonic() + call_limit(timeout, time_left)  # health·systemone 두 번 합쳐서
        if not isinstance(payload.get("state"), dict):
            raise ValueError("payload.state 가 없음")
        opts = payload.get("options") or options
        if not opts:
            raise ValueError("선택지(options)가 없음 — payload 나 JEFF_PROMPT_FILE 확인")
        question = {"type": "choice", "criteria": {k: (v or None) for k, v in opts.items()}}
        text = (payload.get("instructions") or instructions or "").strip()
        if text:
            question["instructions"] = text
        name = payload.get("model") or model or post(url + "/health", None, _left(deadline))["model"]
        body = {"model": name, "state": payload["state"], "questions": {"choice": question}}
        answer = (post(url + "/v1/systemone", body, _left(deadline)).get("answers") or {}).get("choice") or {}
        probs = answer.get("probabilities")
        if not isinstance(probs, dict) or answer.get("choice") not in opts:
            raise ValueError(f"Jeff 답 모양이 다름: {json.dumps(answer, ensure_ascii=False)[:200]}")
        return {"choice": answer["choice"], "probabilities": {k: float(v) for k, v in probs.items()},
                "confidence": float(answer.get("confidence", 0.0)), "model": name}

    return handle


def _left(deadline: float) -> float:
    return max(0.1, deadline - time.monotonic())


def _post(url: str, body: dict | None, timeout: float) -> dict:
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method="POST" if data else "GET",
                                     headers={"content-type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"jeff-serve {error.code}: {error.read().decode(errors='replace')[:200]}") from None
    except (urllib.error.URLError, ConnectionError, TimeoutError) as error:
        raise ModelUnavailable(f"jeff-serve 연결 실패 ({url}): {error}") from None
