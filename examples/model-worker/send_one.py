"""model-worker 왕복 확인: 요청 하나를 큐에 넣고, 워커가 넣은 답을 받아 봅니다.

준비:
  1. 모델 PC에서 워커가 돌고 있어야 합니다 (docs/model-worker.md '3.2 실행·멈추기').
  2. 이 스크립트를 돌리는 PC에 **서버 쪽 권한** 프로필이 있어야 합니다.
     (요청 큐 넣기 + 결과 큐 꺼내기·지우기 + 큐 주소 찾기)
     워커용 키(fintris-jeff·fintris-slm)로는 거부됩니다. 없으면 재혁에게 받습니다.
  3. pip install -r modules/model-worker/requirements.txt

실행 (저장소 맨 위에서):
    python examples/model-worker/send_one.py jeff 명동칼국수
    python examples/model-worker/send_one.py slm "스타벅스 강남점" --profile <서버 권한 프로필>

큐는 이름으로 찾으므로 .env는 필요 없습니다.
"""

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "modules" / "model-worker"))

import boto3  # noqa: E402
from sqs_bridge import ModelQueue, ModelTimeout  # noqa: E402  (위에서 경로를 넣은 뒤 import)

QUEUES = {"jeff": "fintris-dev-jeff-requests", "slm": "fintris-dev-slm-requests"}
REPLIES = "fintris-dev-agent-replies"


def payload(task: str, name: str) -> dict:
    if task == "jeff":  # AI 서버 분류 모듈이 보내는 것과 같은 칸
        return {"state": {"merchant_or_payee": name, "amount": 9000, "direction": "OUT",
                          "account_type": "CARD", "occurred_at": datetime.now().strftime("%Y-%m-%dT%H:%M")}}
    return {"name": name}


def main() -> None:
    parser = argparse.ArgumentParser(description="요청 하나 보내고 답 받기")
    parser.add_argument("task", choices=sorted(QUEUES))
    parser.add_argument("name", help="가맹점 이름")
    parser.add_argument("--profile", help="서버 쪽 권한 AWS 프로필 (비우면 기본 프로필)")
    parser.add_argument("--timeout", type=float, default=15)
    args = parser.parse_args()

    sqs = boto3.Session(profile_name=args.profile, region_name="ap-northeast-2").client("sqs")
    url = lambda name: sqs.get_queue_url(QueueName=name)["QueueUrl"]  # noqa: E731
    queue = ModelQueue(sqs, {args.task: url(QUEUES[args.task])}, url(REPLIES), log=print)

    started = time.perf_counter()
    try:
        res = queue.call(args.task, payload(args.task, args.name), timeout=args.timeout)
    except ModelTimeout:
        print(f"{args.timeout:.0f}초 안에 답이 없습니다. 모델 PC의 워커와 모델 서버가 켜져 있는지 확인하세요.")
        sys.exit(1)
    finally:
        queue.stop(1)
    seconds = time.perf_counter() - started
    print(f"ok={res.ok} {seconds:.2f}초")
    print(res.result if res.ok else f"오류: {res.error} (retryable={res.retryable})")


if __name__ == "__main__":
    main()
