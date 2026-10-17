"""jeff-serve 와 HTTP 로 대화하는 얇은 클라이언트.

Jeff 공식 클라이언트(vendor/jeff/src/jeff/client.py)는 Python 3.12 문법이라 웹 UI 환경(3.11)에서 못 불러온다.
그래서 같은 통신 규격(/health, /v1/systemone)만 표준 라이브러리로 따라 만들었다. torch 없음.
루프는 이 파일을 직접 모르고 engines/base.py 의 모양(health·choose)으로만 부른다.
오류는 전부 JeffUnavailable(= EngineUnavailable, 한국어 안내)로 바꾼다.
"""

import json
import os
import time
import urllib.error
import urllib.request

from engines.base import Decision, EngineUnavailable, Health, TrainParams, TrainStatus
from engines.jeff.trainer import WslTrainer

START_HINT = (
    "켜는 법 (jeff-webui 폴더에서, WSL 안에서 v1.3 베이스 + 학습한 어댑터): "
    "`wsl -d Ubuntu-24.04 -u user -- bash engines/jeff/wsl/serve.sh`"
)


class JeffUnavailable(EngineUnavailable):
    """Jeff 쪽 실패. 루프는 EngineUnavailable 로 잡는다."""


class JeffClient:
    name = "Jeff"

    def __init__(self, url: str, model: str = "", timeout: float = 30.0, train: dict | None = None):
        self.url, self.model, self.timeout = url.rstrip("/"), model, timeout
        self.trainer = WslTrainer(train, self.reload_adapters)

    @classmethod
    def from_settings(cls, settings: dict) -> "JeffClient":
        """settings = config/settings.yaml 의 engines.jeff. 환경변수 JEFF_URL·JEFF_MODEL 이 우선."""
        return cls(os.environ.get("JEFF_URL") or settings.get("url") or "http://127.0.0.1:8765",
                   os.environ.get("JEFF_MODEL") or settings.get("model") or "",
                   float(settings.get("timeout", 30)), settings.get("train"))

    # 학습은 WSL 담당 (engines/jeff/trainer.py)
    def start_training(self, params: TrainParams) -> None:
        self.trainer.start_training(params)

    def training_status(self) -> TrainStatus:
        return self.trainer.training_status()

    def stop_training(self) -> None:
        self.trainer.stop_training()

    def reload_adapters(self) -> None:
        """서버가 어댑터 폴더를 다시 읽게 (새로 학습한 어댑터가 모델 목록에 나오게)."""
        self._send("POST", "/v1/adapters/reload", {})

    def _send(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        headers = {"accept": "application/json"}
        if data is not None:
            headers["content-type"] = "application/json"
        request = urllib.request.Request(self.url + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                text = response.read().decode()
        except urllib.error.HTTPError as error:
            with error:
                raise _http_error(error.code, error.read().decode(errors="replace"), error.headers) from None
        except TimeoutError as error:
            raise JeffUnavailable(f"Jeff 응답이 {self.timeout:.0f}초 안에 안 왔어. GPU가 다른 작업(학습 등)으로 바쁜지 확인해 줘.") from error
        except (urllib.error.URLError, ConnectionError) as error:
            raise JeffUnavailable(f"jeff-serve({self.url})에 연결 못 했어. 서버가 꺼져 있는 것 같아. {START_HINT}") from error
        try:
            return json.loads(text)
        except json.JSONDecodeError as error:
            raise JeffUnavailable(f"Jeff가 JSON이 아닌 답을 보냈어: {text[:200]}") from error

    def server_health(self) -> dict:
        """jeff-serve /health 원본: {"status": "ready"|"loading", "model": ..., "max_options": ...}"""
        return self._send("GET", "/health")

    def health(self) -> Health:
        """엔진 공통 모양의 상태 (화면 맨 위 상태줄)."""
        try:
            h = self.server_health()
        except JeffUnavailable:
            return Health("off", START_HINT, self.url)
        if h.get("status") != "ready":
            return Health("loading", "모델 불러오는 중", self.url)
        adapters = h.get("adapters") or {}
        extra = f" · 학습한 어댑터 {len(adapters)}개" if adapters else ""
        return Health("ready", f"모델 `{h.get('model')}`{extra} · 선택지 최대 {h.get('max_options')}개", self.url)

    def models(self) -> list[str]:
        """맨 앞 = 베이스(학습 전), 그 뒤 = 서버에 올라간 어댑터 (이름순)."""
        try:
            h = self.server_health()
        except JeffUnavailable:
            return []
        if h.get("status") != "ready":
            return []
        return [h["model"], *sorted(h.get("adapters") or {})]

    def resolve_model(self) -> str:
        """설정에 모델 이름이 없으면 서버가 알려주는 이름을 쓴다 (이름 틀려서 422 나는 걸 막으려고)."""
        return self.model or self.server_health()["model"]

    def choose(self, state: dict, options: dict[str, str], instructions: str = "", orders: int = 1,
               model: str = "") -> Decision:
        """선택지 중 입력(state)에 가장 맞는 것과 선택지별 확률. orders=2 면 선택지 순서를 뒤집어 한 번 더 묻고 평균."""
        if not options:
            raise JeffUnavailable("선택지가 하나도 없어. 프로필의 labels.yaml 을 확인해 줘.")
        question = {"type": "choice", "criteria": {key: (desc or None) for key, desc in options.items()}}
        if instructions.strip():
            question["instructions"] = instructions.strip()
        body = {"model": model or self.resolve_model(), "state": state, "questions": {"choice": question}}
        if orders == 2:
            body["orders"] = 2
        started = time.perf_counter()
        response = self._send("POST", "/v1/systemone", body)
        elapsed_ms = (time.perf_counter() - started) * 1000
        answer = (response.get("answers") or {}).get("choice") or {}
        probs = answer.get("probabilities")
        if not isinstance(probs, dict) or set(probs) != set(options) or answer.get("choice") not in options:
            raise JeffUnavailable(f"Jeff 답의 모양이 예상과 달라: {json.dumps(answer, ensure_ascii=False)[:300]}")
        return Decision(answer["choice"], {key: float(probs[key]) for key in options},
                        float(answer.get("confidence", 0.0)), elapsed_ms, body, response)


def _http_error(status: int, text: str, headers) -> JeffUnavailable:
    try:
        detail = json.loads(text).get("detail", text)
    except (json.JSONDecodeError, AttributeError):
        detail = text
    shown = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)
    if status == 503:
        return JeffUnavailable("Jeff가 아직 모델을 불러오는 중이야. 잠깐 뒤에 다시 눌러 줘.")
    if status == 529:
        retry = headers.get("retry-after") if headers else None
        return JeffUnavailable(f"Jeff가 다른 요청을 처리 중이야. {retry or '몇'}초 뒤에 다시 눌러 줘.")
    if status == 401:
        return JeffUnavailable("서버에 JEFF_API_KEY가 걸려 있어. 웹 UI는 아직 키를 안 보내.")
    if status == 422:
        if "model" in shown:
            return JeffUnavailable(f"서버가 그 모델 이름을 몰라. settings.yaml 의 engines.jeff.model 을 비우면 서버 이름을 자동으로 써. ({shown[:200]})")
        if "at most" in shown or "too_long" in shown:
            return JeffUnavailable(f"선택지가 이 모델이 다룰 수 있는 개수보다 많아. ({shown[:200]})")
        return JeffUnavailable(f"Jeff가 요청 형식을 거절했어: {shown[:300]}")
    return JeffUnavailable(f"Jeff 서버 오류 {status}: {shown[:300]}")

