"""11/18 요청 관리 구조: 대기 목록 먼저 등록 · 결과 큐는 Consumer 하나 · 응답 검증 4갈래."""
import threading
import time

import pytest

from sqs_bridge import ModelQueue, ModelResult, ModelTimeout, ModelUnavailable
from sqs_bridge import worker

from test_bridge import REPLY, REQ, AutoReplySQS, FakeSQS

SLM_REQ = "q-slm"
quiet = lambda _: None  # noqa: E731


def make_queue(sqs, **kw):
    return ModelQueue(sqs, {"jeff": REQ, "slm": SLM_REQ}, REPLY, wait_seconds=0, log=quiet, **kw)


def answer_all(sqs, queue_url, handlers, reverse=False):
    """워커 한 번 돌리기. reverse=True면 받은 순서 반대로 답함."""
    messages = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=10).get("Messages", [])
    for m in reversed(messages) if reverse else messages:
        worker.handle_one(sqs, queue_url, m, handlers, log=quiet)
    return len(messages)


def test_concurrent_calls_each_get_own_answer():
    sqs = FakeSQS()
    q = make_queue(sqs)
    results: dict[str, ModelResult] = {}

    def run(name, task):
        results[name] = q.call(task, {"name": name}, timeout=5)

    callers = [threading.Thread(target=run, args=(n, t)) for n, t in
               [("a", "jeff"), ("b", "jeff"), ("c", "jeff"), ("d", "slm")]]
    for t in callers:
        t.start()
    echo = {"jeff": lambda p, _t=None: {"echo": p["name"]}, "slm": lambda p, _t=None: {"echo": p["name"]}}
    deadline = time.time() + 3
    while q.pending_count() < 4 and time.time() < deadline:
        time.sleep(0.01)
    answer_all(sqs, REQ, echo, reverse=True)  # 순서 뒤섞어 답함
    answer_all(sqs, SLM_REQ, echo)
    for t in callers:
        t.join(5)
    q.stop(1)
    assert {n: r.result["echo"] for n, r in results.items()} == {"a": "a", "b": "b", "c": "c", "d": "d"}
    assert q.pending_count() == 0 and sqs.bodies(REPLY) == [] and not sqs.in_flight


def test_orphan_and_bad_replies_are_deleted():
    sqs = FakeSQS()
    q = make_queue(sqs)
    sqs.send_message(QueueUrl=REPLY, MessageBody=ModelResult("nobody", True, result={}).to_json())
    sqs.send_message(QueueUrl=REPLY, MessageBody="not json")
    assert q.poll_once() == 2
    assert sqs.bodies(REPLY) == [] and not sqs.in_flight


def test_timeout_removes_pending_and_late_reply_becomes_orphan():
    sqs = FakeSQS()
    q = make_queue(sqs)
    with pytest.raises(ModelTimeout):
        q.call("jeff", {}, timeout=0.2)
    assert q.pending_count() == 0
    q.stop(1)
    [m] = sqs.receive_message(QueueUrl=REQ)["Messages"]
    m["Body"] = m["Body"].replace('"expires_at": ', '"expires_at": 0, "_old": ')  # 기한 무시하고 늦게 처리
    worker.handle_one(sqs, REQ, m, {"jeff": lambda p, _t=None: {}}, log=quiet)
    assert q._handle(sqs.receive_message(QueueUrl=REPLY)["Messages"][0]) == "orphan"


def test_retryable_failure_is_sent_again_then_succeeds():
    calls = []

    def flaky(payload, _t=None):
        calls.append(payload)
        if len(calls) == 1:
            raise ModelUnavailable("jeff-serve 연결 실패")
        return {"choice": "slm"}

    sqs = AutoReplySQS({"jeff": flaky})
    q = make_queue(sqs)
    res = q.call("jeff", {"name": "스타벅스"}, timeout=5)
    q.stop(1)
    assert res.ok and res.result == {"choice": "slm"} and len(calls) == 2


def test_retry_stops_after_max_retries():
    calls = []

    def down(payload, _t=None):
        calls.append(payload)
        raise ModelUnavailable("꺼짐")

    sqs = AutoReplySQS({"jeff": down})
    q = make_queue(sqs, max_retries=1)
    res = q.call("jeff", {}, timeout=5)
    q.stop(1)
    assert not res.ok and res.retryable and len(calls) == 2  # 처음 1번 + 재시도 1번


def test_non_retryable_failure_is_delivered_at_once():
    calls = []

    def bad(payload, _t=None):
        calls.append(payload)
        raise ValueError("이름 없음")

    sqs = AutoReplySQS({"jeff": bad})
    q = make_queue(sqs)
    res = q.call("jeff", {}, timeout=5)
    q.stop(1)
    assert not res.ok and not res.retryable and len(calls) == 1


def test_send_failure_removes_pending():
    sqs = FakeSQS()
    sqs.fail_send = True
    q = make_queue(sqs)
    with pytest.raises(ConnectionError):
        q.call("jeff", {}, timeout=1)
    q.stop(1)
    assert q.pending_count() == 0


def test_unknown_task_and_single_consumer_thread():
    sqs = FakeSQS()
    q = make_queue(sqs)
    with pytest.raises(KeyError):
        q.call("gemini", {})
    assert q._thread is None  # 모르는 일은 스레드 띄우기 전에 막음
    q.start()
    first = q._thread
    q.start()
    assert q._thread is first  # 몇 번 불러도 결과 큐 읽는 스레드는 하나
    q.stop(1)
    assert not first.is_alive()


def test_worker_marks_connection_failure_retryable():
    assert worker._retryable(ModelUnavailable("x")) and worker._retryable(ConnectionError())
    assert not worker._retryable(ValueError()) and not worker._retryable(RuntimeError("sLM 401"))
