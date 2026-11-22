"""통합 워커 (python -m sqs_bridge): 설정으로 켜고 끄기, 큐마다 스레드, 하나 죽으면 전체 멈춤."""
import threading
import time

import pytest

from sqs_bridge import ModelRequest
from sqs_bridge.__main__ import Lane, build_lanes, check_access, run_lanes
from test_bridge import FakeSQS

REPLY = "q-replies"


class LockedSQS(FakeSQS):
    """FakeSQS는 이미 잠금이 있음. 빈 큐면 잠깐 쉼(롱 폴링 흉내)만 더함."""

    def receive_message(self, **kw):
        got = super().receive_message(**kw)
        if not got:
            time.sleep(0.01)
        return got

    def get_queue_attributes(self, QueueUrl, AttributeNames):
        if QueueUrl.startswith("denied"):
            raise PermissionError("AccessDenied")
        return {"Attributes": {"QueueArn": "arn"}}


ENV = {"JEFF_REQUESTS_URL": "q-jeff", "SLM_REQUESTS_URL": "q-slm", "SLM_URL": "http://127.0.0.1:8000/v1",
       "SLM_MODEL": "qwen"}


def test_build_lanes_follows_flags():
    assert build_lanes({**ENV}) == []
    assert [lane.task for lane in build_lanes({**ENV, "JEFF_ENABLED": "true"})] == ["jeff"]
    assert [lane.task for lane in build_lanes({**ENV, "SLM_ENABLED": "1"})] == ["slm"]
    both = build_lanes({**ENV, "JEFF_ENABLED": "true", "SLM_ENABLED": "TRUE"})
    assert [(lane.task, lane.queue_url) for lane in both] == [("jeff", "q-jeff"), ("slm", "q-slm")]


def test_enabled_lane_without_setting_stops_at_start():
    with pytest.raises(SystemExit, match="SLM_URL"):
        build_lanes({"SLM_ENABLED": "true", "SLM_REQUESTS_URL": "q-slm", "SLM_MODEL": "qwen"})
    with pytest.raises(SystemExit, match="JEFF_REQUESTS_URL"):
        build_lanes({"JEFF_ENABLED": "true"})


def test_check_access_reports_queue_without_permission():
    sqs = LockedSQS()
    check_access(sqs, [Lane("jeff", "q-jeff", lambda p, _t=None: p)], log=lambda _: None)
    with pytest.raises(SystemExit, match="slm 큐에 접근 못 함"):
        check_access(sqs, [Lane("slm", "denied-slm", lambda p, _t=None: p)], log=lambda _: None)


def _wait(cond, seconds=3.0):
    end = time.time() + seconds
    while time.time() < end and not cond():
        time.sleep(0.01)
    return cond()


def test_each_queue_has_its_own_thread_and_same_reply_queue():
    sqs = LockedSQS()
    slow = threading.Event()

    def jeff(payload, _t=None):  # Jeff가 막혀 있어도 sLM 줄은 계속 돌아야 함
        slow.wait(2)
        return {"choice": "slm"}

    lanes = [Lane("jeff", "q-jeff", jeff), Lane("slm", "q-slm", lambda p, _t=None: {"category_code": "CAFE"})]
    jeff_req = ModelRequest("jeff", {"state": {}}, REPLY)
    slm_req = ModelRequest("slm", {"name": "스타벅스"}, REPLY)
    sqs.send_message(QueueUrl="q-jeff", MessageBody=jeff_req.to_json())
    sqs.send_message(QueueUrl="q-slm", MessageBody=slm_req.to_json())

    stop = threading.Event()
    runner = threading.Thread(target=lambda: results.append(run_lanes(lambda: sqs, lanes, stop, log=lambda _: None)))
    results: list[bool] = []
    runner.start()
    assert _wait(lambda: any(b["request_id"] == slm_req.request_id for b in sqs.bodies(REPLY)))
    assert all(b["request_id"] != jeff_req.request_id for b in sqs.bodies(REPLY))  # Jeff는 아직
    slow.set()
    assert _wait(lambda: len(sqs.bodies(REPLY)) == 2)
    stop.set()
    runner.join(3)
    assert results == [True]


def test_one_lane_crash_stops_all_and_reports_failure():
    sqs = LockedSQS()
    calls = {"n": 0}

    def broken_sqs():
        calls["n"] += 1
        if calls["n"] == 1:
            class Dead:
                def receive_message(self, **kw):
                    raise KeyboardInterrupt  # worker.run이 못 잡는 오류 → 줄이 죽음
            return Dead()
        return sqs

    lanes = [Lane("jeff", "q-jeff", lambda p, _t=None: p), Lane("slm", "q-slm", lambda p, _t=None: p)]
    stop = threading.Event()
    logs: list[str] = []
    assert run_lanes(broken_sqs, lanes, stop, log=logs.append) is False
    assert stop.is_set() and any("오류로 멈춤" in line for line in logs)
