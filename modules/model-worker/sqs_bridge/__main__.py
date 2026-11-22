"""통합 워커: 이 PC에서 켠 큐(Jeff·sLM)만 꺼내 이 PC의 모델에 묻고 답장 큐에 넣는다.

실행:  python -m sqs_bridge      (설정은 환경변수나 .env)

같은 코드를 PC마다 .env만 바꿔 띄운다.
  재혁 PC   JEFF_ENABLED=true  SLM_ENABLED=false
  승현 PC   JEFF_ENABLED=false SLM_ENABLED=true
큐마다 스레드 하나 (한 큐의 20초 대기가 다른 큐를 막지 않게).
한 스레드라도 죽으면 전체를 멈춰서 밖(사람·서비스 관리자)이 알게 한다.
"""
from __future__ import annotations

import os
import signal
import sys
import threading
from dataclasses import dataclass
from typing import Callable, Mapping

from . import jeff, slm, worker
from .jeff import load_env

TRUE = {"1", "true", "yes", "on"}


@dataclass
class Lane:
    task: str
    queue_url: str
    handler: Callable[[dict, "float | None"], dict]


def enabled(env: Mapping[str, str], name: str) -> bool:
    return env.get(name, "").strip().lower() in TRUE


def _need(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise SystemExit(f"설정 없음: {name}")
    return value


def build_lanes(env: Mapping[str, str] = os.environ) -> list[Lane]:
    """켠 일마다 (큐 주소, 처리 함수). 켰는데 필요한 설정이 없으면 바로 멈춤."""
    lanes: list[Lane] = []
    if enabled(env, "JEFF_ENABLED"):
        options, instructions = jeff.load_prompt(env.get("JEFF_PROMPT_FILE", ""))
        lanes.append(Lane("jeff", _need(env, "JEFF_REQUESTS_URL"), jeff.make_handler(
            env.get("JEFF_URL", "http://127.0.0.1:8765"), env.get("JEFF_MODEL", ""),
            float(env.get("JEFF_TIMEOUT", "30")), options, instructions)))
    if enabled(env, "SLM_ENABLED"):
        lanes.append(Lane("slm", _need(env, "SLM_REQUESTS_URL"), slm.make_handler(
            _need(env, "SLM_URL"), env.get("SLM_API_KEY", ""), _need(env, "SLM_MODEL"),
            float(env.get("SLM_TIMEOUT", "30")), env.get("SLM_PROMPT", slm.DEFAULT_PROMPT))))
    return lanes


def check_access(sqs, lanes: list[Lane], log: Callable[[str], None] = print) -> None:
    """시작할 때 켠 큐마다 권한 확인 → 키와 설정이 안 맞으면 바로 알림."""
    for lane in lanes:
        try:
            sqs.get_queue_attributes(QueueUrl=lane.queue_url, AttributeNames=["QueueArn"])
        except Exception as error:
            raise SystemExit(f"{lane.task} 큐에 접근 못 함: {type(error).__name__}. "
                             f"이 PC의 AWS 키 권한과 {lane.task.upper()}_REQUESTS_URL 확인") from None
        log(f"{lane.task} 큐 확인됨")


def run_lanes(make_sqs: Callable[[], object], lanes: list[Lane], stop: threading.Event,
              log: Callable[[str], None] = print) -> bool:
    """큐마다 스레드. 모두 정상 종료면 True, 하나라도 죽으면 나머지도 멈추고 False."""
    failed: list[str] = []

    def lane_main(lane: Lane) -> None:
        try:
            worker.run(make_sqs(), lane.queue_url, {lane.task: lane.handler}, stop=stop.is_set,
                       log=lambda text: log(f"[{lane.task}] {text}"))
        except BaseException as error:  # 어떤 이유든 한 줄이 죽으면 전체 멈춤
            failed.append(lane.task)
            log(f"[{lane.task}] 오류로 멈춤: {type(error).__name__}: {error}")
            stop.set()

    threads = [threading.Thread(target=lane_main, args=(lane,), name=f"worker-{lane.task}", daemon=True)
               for lane in lanes]
    for thread in threads:
        thread.start()
    while any(thread.is_alive() for thread in threads):
        for thread in threads:
            thread.join(timeout=0.5)
    return not failed


def main() -> None:
    load_env()
    lanes = build_lanes()
    if not lanes:
        raise SystemExit("켠 일이 없음: JEFF_ENABLED·SLM_ENABLED 중 하나를 true로")
    import boto3

    session = boto3.Session(profile_name=os.environ.get("AWS_PROFILE") or None,
                            region_name=os.environ.get("AWS_REGION", "ap-northeast-2"))

    def make_sqs():  # 스레드마다 클라이언트 따로
        return session.client("sqs")

    check_access(make_sqs(), lanes)
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: (stop.set(), print("멈추는 중… (최대 20초)")))
    ok = run_lanes(make_sqs, lanes, stop)
    print("멈춤" if ok else "오류로 멈춤")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
