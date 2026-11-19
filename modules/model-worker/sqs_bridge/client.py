"""서버 쪽: 요청 큐에 넣고, 결과 큐는 Result Consumer 스레드 하나만 읽어 주인에게 넘김.

11/18 요청 관리 구조 (05_아키텍처/AI_요청관리_구조/ai-request-flow-1118.mmd):
- Request Manager (call): 요청 ID를 대기 목록에 **먼저** 등록 → 모델별 큐로 보냄 → 자기 Future만 기다림.
- Result Consumer (poll_once): 결과 큐를 여기서만 읽음. 하나씩 처리하고 지움.
- 응답 검증 4갈래: 성공 → 전달 / 재시도 불가 → 실패 전달 / 재시도 가능 → 같은 요청 다시 보냄 / 주인 없음 → 삭제.
- 기한은 대기 목록 한 곳에서만 봄. 꺼내기는 pending.pop 한 줄 → 시간 초과 쪽과 결과 쪽 중 먼저 꺼낸 쪽만 가짐.

조건: 프로세스 하나에 ModelQueue 하나만 만들어 모든 일(jeff·slm)이 같이 씀.
같은 결과 큐를 읽는 ModelQueue가 둘이면 서로의 답을 "주인 없음"으로 지워 버림.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Callable

from .messages import MessageError, ModelRequest, ModelResult


class ModelTimeout(TimeoutError):
    """기다리는 시간 안에 답이 없음 (집 PC 꺼짐 등) → 호출한 쪽이 대체 처리(예: 사용자에게 묻기)."""


@dataclass
class _Pending:
    request: ModelRequest
    queue_url: str
    future: Future = field(default_factory=Future)
    retries: int = 0


class ModelQueue:
    def __init__(self, sqs, request_urls: dict[str, str], reply_url: str, *, max_retries: int = 1,
                 wait_seconds: int = 20, log: Callable[[str], None] = print):
        self.sqs, self.request_urls, self.reply_url = sqs, request_urls, reply_url
        self.max_retries, self.wait_seconds, self.log = max_retries, wait_seconds, log
        self._pending: dict[str, _Pending] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()

    # ── Request Manager ──────────────────────────────────────────────
    def call(self, task: str, payload: dict, timeout: float = 10.0) -> ModelResult:
        if task not in self.request_urls:
            raise KeyError(f"모르는 일: {task}")
        self.start()
        deadline = time.time() + timeout
        req = ModelRequest(task=task, payload=payload, reply_to=self.reply_url, expires_at=deadline)
        entry = _Pending(req, self.request_urls[task])
        self._pending[req.request_id] = entry  # 보내기 전에 등록 → 답이 아무리 빨라도 주인을 찾음
        try:
            self.sqs.send_message(QueueUrl=entry.queue_url, MessageBody=req.to_json())
        except Exception:
            self._pending.pop(req.request_id, None)
            raise
        try:
            return entry.future.result(timeout=max(0.0, deadline - time.time()))
        except FutureTimeout:
            if self._pending.pop(req.request_id, None) is None and entry.future.done():
                return entry.future.result()  # 시간 초과 직전에 결과 쪽이 먼저 꺼내 넘김
            raise ModelTimeout(f"{task} 답이 {timeout:.0f}초 안에 안 옴 ({req.request_id})") from None

    def pending_count(self) -> int:
        return len(self._pending)

    # ── Result Consumer ──────────────────────────────────────────────
    def start(self) -> None:
        """결과 큐 읽는 스레드를 하나만 띄움. 서버 시작 때 불러도 되고, 첫 call()이 알아서 부름."""
        with self._start_lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="model-result-consumer", daemon=True)
            self._thread.start()

    def stop(self, timeout: float | None = None) -> None:
        """서버 끌 때. 롱 폴링 중이면 최대 wait_seconds 뒤에 멈춤."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                if self.poll_once() == 0:
                    self._stop.wait(0.05)  # 진짜 SQS는 롱 폴링이라 여기 안 옴. 가짜 큐에서 헛돌기 방지
            except Exception as error:  # 인터넷 끊김 등 → 잠깐 쉬고 다시
                self.log(f"결과 받기 실패: {type(error).__name__}: {error}")
                self._stop.wait(5)

    def poll_once(self) -> int:
        """결과 큐에서 최대 10개 꺼내 처리. 처리한 개수를 돌려줌."""
        got = self.sqs.receive_message(QueueUrl=self.reply_url, MaxNumberOfMessages=10,
                                       WaitTimeSeconds=self.wait_seconds)
        messages = got.get("Messages", [])
        for message in messages:
            self._handle(message)
        return len(messages)

    def _handle(self, message: dict) -> str:
        """응답 검증 4갈래. 돌려주는 값은 테스트·로그용: bad | orphan | retry | delivered"""
        receipt = message["ReceiptHandle"]
        try:
            res = ModelResult.from_json(message.get("Body", ""))
        except MessageError as error:
            self.log(f"결과 버림(양식 오류): {error}")
            outcome = "bad"
        else:
            outcome = self._route(res)
        self.sqs.delete_message(QueueUrl=self.reply_url, ReceiptHandle=receipt)
        return outcome

    def _route(self, res: ModelResult) -> str:
        entry = self._pending.get(res.request_id)
        if entry is None:  # 시간 초과로 이미 포기했거나 다른 서버의 답
            return "orphan"
        if (not res.ok and res.retryable and entry.retries < self.max_retries
                and not entry.request.expired()):
            entry.retries += 1
            try:  # 같은 요청 ID·같은 기한으로 다시 (늦게 온 원래 답은 주인 없음으로 지워짐)
                self.sqs.send_message(QueueUrl=entry.queue_url, MessageBody=entry.request.to_json())
                self.log(f"재시도 {entry.retries}/{self.max_retries}: {res.request_id} ({res.error})")
                return "retry"
            except Exception as error:
                res = ModelResult(res.request_id, False, error=f"재시도 보내기 실패: {error}"[:500])
        owner = self._pending.pop(res.request_id, None)
        if owner is None:  # 그 사이 시간 초과 쪽이 먼저 꺼냄
            return "orphan"
        owner.future.set_result(res)
        return "delivered"
