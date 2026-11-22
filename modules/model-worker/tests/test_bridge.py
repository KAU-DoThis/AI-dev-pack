import functools
import itertools
import json
import threading
import time

import pytest

from sqs_bridge import ModelQueue, ModelRequest, ModelResult, ModelTimeout
from sqs_bridge import worker
from sqs_bridge.jeff import make_handler


def _locked(method):
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self.lock:
            return method(self, *args, **kwargs)
    return wrapper


class FakeSQS:
    """큐 이름 → 메시지 목록. 받으면 숨김(in_flight), 지우면 사라짐, 숨김 0이면 되돌아옴.
    결과 받는 스레드와 call()이 같이 만지므로 잠금으로 감쌈."""

    lock: threading.RLock

    def __init__(self):
        self.lock = threading.RLock()
        self.queues: dict[str, list[dict]] = {}
        self.in_flight: dict[str, tuple[str, dict]] = {}
        self.ids = itertools.count()
        self.fail_send = False

    @_locked
    def send_message(self, QueueUrl, MessageBody):
        if self.fail_send:
            raise ConnectionError("network down")
        self.queues.setdefault(QueueUrl, []).append({"Body": MessageBody})

    @_locked
    def receive_message(self, QueueUrl, MaxNumberOfMessages=1, WaitTimeSeconds=0):
        q = self.queues.setdefault(QueueUrl, [])
        out = []
        while q and len(out) < MaxNumberOfMessages:
            msg = q.pop(0)
            receipt = f"r{next(self.ids)}"
            self.in_flight[receipt] = (QueueUrl, msg)
            out.append({**msg, "ReceiptHandle": receipt})
        return {"Messages": out} if out else {}

    @_locked
    def delete_message(self, QueueUrl, ReceiptHandle):
        self.in_flight.pop(ReceiptHandle)

    @_locked
    def change_message_visibility(self, QueueUrl, ReceiptHandle, VisibilityTimeout):
        url, msg = self.in_flight.pop(ReceiptHandle)
        self.queues[url].append(msg)

    def bodies(self, url):
        return [json.loads(m["Body"]) for m in self.queues.get(url, [])]


REQ, REPLY = "q-jeff", "q-replies"


def put(sqs, req: ModelRequest):
    sqs.send_message(QueueUrl=REQ, MessageBody=req.to_json())


def drain(sqs, handlers):
    for m in sqs.receive_message(QueueUrl=REQ, MaxNumberOfMessages=10).get("Messages", []):
        worker.handle_one(sqs, REQ, m, handlers, log=lambda _: None)


def test_worker_replies_and_deletes():
    sqs = FakeSQS()
    req = ModelRequest("jeff", {"state": {"x": 1}}, REPLY, expires_at=time.time() + 60)
    put(sqs, req)
    drain(sqs, {"jeff": lambda p, _t=None: {"choice": "slm", "seen": p["state"]["x"]}})
    [reply] = sqs.bodies(REPLY)
    assert reply["request_id"] == req.request_id and reply["ok"] is True
    assert reply["result"] == {"choice": "slm", "seen": 1} and reply["expires_at"] == req.expires_at
    assert not sqs.queues[REQ] and not sqs.in_flight


def test_worker_handler_error_becomes_error_reply():
    sqs = FakeSQS()
    put(sqs, ModelRequest("jeff", {}, REPLY))
    drain(sqs, {"jeff": lambda p, _t=None: 1 / 0})
    [reply] = sqs.bodies(REPLY)
    assert reply["ok"] is False and "ZeroDivisionError" in reply["error"]


def test_worker_unknown_task():
    sqs = FakeSQS()
    put(sqs, ModelRequest("slm", {}, REPLY))
    drain(sqs, {"jeff": lambda p, _t=None: {}})
    assert sqs.bodies(REPLY)[0]["ok"] is False


def test_worker_drops_expired_and_bad_without_reply():
    sqs = FakeSQS()
    put(sqs, ModelRequest("jeff", {}, REPLY, expires_at=time.time() - 1))
    sqs.send_message(QueueUrl=REQ, MessageBody="not json")
    sqs.send_message(QueueUrl=REQ, MessageBody=json.dumps({"task": "jeff"}))
    called = []
    drain(sqs, {"jeff": lambda p, _t=None: called.append(p) or {}})
    assert not called and not sqs.bodies(REPLY) and not sqs.in_flight


def test_worker_keeps_request_when_reply_fails():
    sqs = FakeSQS()
    put(sqs, ModelRequest("jeff", {}, REPLY))
    msg = sqs.receive_message(QueueUrl=REQ)["Messages"][0]
    sqs.fail_send = True
    assert worker.handle_one(sqs, REQ, msg, {"jeff": lambda p, _t=None: {}}, log=lambda _: None) == "send-failed"
    assert msg["ReceiptHandle"] in sqs.in_flight  # 안 지움 → 숨김 시간 뒤 다시 처리


class AutoReplySQS(FakeSQS):
    """요청이 들어오면 워커가 바로 처리하는 것처럼 동작."""

    def __init__(self, handlers, extra_replies=()):
        super().__init__()
        self.handlers = handlers
        for body in extra_replies:
            self.queues.setdefault(REPLY, []).append({"Body": body})

    @_locked
    def send_message(self, QueueUrl, MessageBody):
        super().send_message(QueueUrl, MessageBody)
        if QueueUrl == REQ and self.handlers:
            drain(self, self.handlers)


def test_client_gets_own_reply_and_deletes_orphans():
    other = ModelResult("someone-else", True, result={}, expires_at=time.time() + 60).to_json()
    stale = ModelResult("old", True, result={}, expires_at=time.time() - 1).to_json()
    sqs = AutoReplySQS({"jeff": lambda p, _t=None: {"choice": "ask"}}, [other, stale])
    q = ModelQueue(sqs, {"jeff": REQ}, REPLY, wait_seconds=0, log=lambda _: None)
    try:
        res = q.call("jeff", {"state": {}}, timeout=5)
    finally:
        q.stop(1)
    assert res.ok and res.result == {"choice": "ask"}
    assert sqs.bodies(REPLY) == []  # 대기 목록에 없는 답은 주인 없음 → 지움 (돌려놓지 않음)
    assert not sqs.in_flight and q.pending_count() == 0


def test_client_times_out_when_nobody_answers():
    sqs = AutoReplySQS({})
    started = time.time()
    q = ModelQueue(sqs, {"jeff": REQ}, REPLY, wait_seconds=0)
    with pytest.raises(ModelTimeout):
        q.call("jeff", {"state": {}}, timeout=0.3)
    q.stop(1)
    assert time.time() - started < 2
    [req] = sqs.bodies(REQ)
    assert req["expires_at"] <= time.time()  # 늦게 켜진 워커는 이 요청을 버림


def test_jeff_handler_builds_request():
    calls = []

    def post(url, body, timeout):
        calls.append((url, body))
        if url.endswith("/health"):
            return {"model": "jeff-qwen3.5-0.8b"}
        return {"answers": {"choice": {"choice": "slm", "probabilities": {"slm": 0.8, "ask": 0.2}, "confidence": 0.8}}}

    handle = make_handler("http://jeff/", options={"slm": "small model", "ask": "ask user"},
                          instructions=" decide ", post=post)
    out = handle({"state": {"merchant_or_payee": "명동칼국수"}})
    assert out == {"choice": "slm", "probabilities": {"slm": 0.8, "ask": 0.2}, "confidence": 0.8,
                   "model": "jeff-qwen3.5-0.8b"}
    url, body = calls[-1]
    assert url == "http://jeff/v1/systemone"
    assert body["questions"]["choice"] == {"type": "choice", "criteria": {"slm": "small model", "ask": "ask user"},
                                           "instructions": "decide"}


def test_jeff_handler_rejects_bad_payload_and_answer():
    post = lambda url, body, timeout: {"answers": {"choice": {"choice": "auto", "probabilities": {}}}}
    handle = make_handler("http://jeff", model="m", options={"slm": "", "ask": ""}, post=post)
    with pytest.raises(ValueError):
        handle({})
    with pytest.raises(ValueError):
        handle({"state": {}})


# ── sLM 워커 (11/08: 프롬프트·주소·키를 서버에서 워커로 옮김) ──
from sqs_bridge import slm as slm_worker


def slm_post(content, seen=None):
    def post(url, body, headers, timeout):
        if seen is not None:
            seen.append((url, body, headers))
        return {"choices": [{"message": {"content": content}}]}
    return post


@pytest.mark.parametrize("content, code", [
    ('{"category_code":"DELIVERY_DINING"}', "DELIVERY_DINING"),
    ('생각 중... {"category_code":"FOOD"} 그리고 {"category_code":"CAFE"}', "CAFE"),  # 마지막 것
    ('{"category_code":"UNKNOWN"}', "UNKNOWN"),  # 선택지 검사는 서버가 한다
    ("모르겠어요", None),
    ("", None),
    (None, None),
])
def test_slm_handler_parses_last_code(content, code):
    handle = slm_worker.make_handler("http://slm/v1", "k", "m", post=slm_post(content))
    assert handle({"name": "명동칼국수"}) == {"category_code": code, "prompt": slm_worker.DEFAULT_PROMPT}


def test_slm_handler_sends_only_name_type_and_prompt():
    seen = []
    handle = slm_worker.make_handler("http://slm/v1/", "secret", "qwen", post=slm_post('{"category_code":"FOOD"}', seen))
    handle({"name": "명동칼국수", "merchant_type": "일반한식", "amount": 15000})
    url, body, headers = seen[0]
    assert url == "http://slm/v1/chat/completions" and body["model"] == "qwen" and body["temperature"] == 0
    assert body["messages"][0]["content"] == slm_worker.PROMPTS["v4"]
    user = body["messages"][1]["content"]
    assert "명동칼국수" in user and "일반한식" in user and "15000" not in json.dumps(body, ensure_ascii=False)
    assert headers == {"Authorization": "Bearer secret"}


def test_slm_handler_rejects_missing_name():
    handle = slm_worker.make_handler("http://slm/v1", "k", "m", post=slm_post("{}"))
    with pytest.raises(ValueError):
        handle({"name": " "})


def test_slm_worker_round_trip_through_queue():
    sqs = FakeSQS()
    q = ModelQueue(sqs, {"slm": REQ}, REPLY, wait_seconds=0)
    handle = slm_worker.make_handler("http://slm/v1", "k", "m", post=slm_post('{"category_code":"CAFE"}'))
    orig = sqs.receive_message

    def receive(QueueUrl, **kw):  # 서버가 답장을 기다리는 사이 워커가 한 번 돈다
        if QueueUrl == REPLY:
            drain(sqs, {"slm": handle})
        return orig(QueueUrl, **kw)

    sqs.receive_message = receive
    res = q.call("slm", {"name": "블루보틀"}, timeout=5)
    q.stop(1)
    assert res.ok and res.result["category_code"] == "CAFE"


def test_slm_http_error_hides_key():
    import urllib.error
    import urllib.request

    def boom(*a, **k):
        raise urllib.error.HTTPError("http://slm", 401, "Unauthorized", {}, None)

    real = urllib.request.urlopen
    urllib.request.urlopen = boom
    try:
        with pytest.raises(RuntimeError) as err:
            slm_worker._post("http://slm/v1/chat/completions", {}, {"Authorization": "Bearer secret"}, 1)
    finally:
        urllib.request.urlopen = real
    assert "secret" not in str(err.value) and "401" in str(err.value)


def test_worker_limits_model_call_to_time_server_still_waits():
    """서버 기한 10초 > 남은 3초면 모델 호출도 3초 안. 서버가 포기한 요청에 모델을 30초 붙잡지 않는다."""
    seen = []
    sqs = FakeSQS()
    req = ModelRequest("jeff", {"state": {}}, REPLY, expires_at=time.time() + 3)
    sqs.send_message(QueueUrl=REQ, MessageBody=req.to_json())
    drain(sqs, {"jeff": lambda p, t=None: seen.append(t) or {}})
    assert seen and 2 < seen[0] <= 3


def test_jeff_and_slm_handlers_use_shorter_of_setting_and_time_left():
    timeouts = []

    def jeff_post(url, body, timeout):
        timeouts.append(timeout)
        return {"model": "m"} if body is None else {"answers": {"choice": {"choice": "ask", "probabilities": {"ask": 0.9, "slm": 0.1}}}}

    handle = make_handler("http://jeff", options={"slm": "", "ask": ""}, timeout=30, post=jeff_post)
    handle({"state": {}}, 2.0)
    assert len(timeouts) == 2 and all(t <= 2.0 for t in timeouts)  # health + systemone 합쳐서 2초 안
    handle({"state": {}})                                         # 기한 없으면 설정값
    assert timeouts[-1] > 29

    seen = []
    slm_handle = slm_worker.make_handler("http://slm/v1", "k", "m", timeout=30,
                                         post=lambda u, b, h, t: seen.append(t) or {"choices": [{"message": {"content": "{}"}}]})
    slm_handle({"name": "가게"}, 4.0)
    slm_handle({"name": "가게"}, -1.0)   # 이미 지남 → 최소값
    assert seen == [4.0, 0.1]
