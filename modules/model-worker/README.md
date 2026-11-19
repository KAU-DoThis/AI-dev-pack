# model-worker

Jeff·sLM을 SQS 큐로 연결하는 코드입니다. 두 부분으로 되어 있습니다.

- **워커** (`python -m sqs_bridge`): 모델이 있는 PC에서 돕니다. 자기 요청 큐에서 요청을 꺼내 같은 PC의 모델(jeff-serve·vLLM)에 묻고, 답을 결과 큐에 넣습니다.
- **클라이언트** (`sqs_bridge/client.py`의 `ModelQueue`): AI 서버가 씁니다. 요청 큐에 넣고 결과 큐에서 자기 답을 받습니다.

AI 서버와 모델 PC는 직접 연결되지 않고 큐로만 주고받습니다. 워커 코드는 하나이고, PC마다 `.env`만 다르게 씁니다 (Jeff PC는 Jeff 큐, sLM PC는 sLM 큐).

## 빨리 띄우기

```
pip install -r requirements.txt
cp .env.example .env      # 이 PC 것만 true로, 칸 채우기
python -m sqs_bridge
```

설치 방법(AWS 키·모델 서버), `.env` 칸 설명, 입출력, 주의 사항은 **`docs/model-worker.md`**에 있습니다. Jeff로 처음부터 끝까지 보여 주고, sLM은 바꿀 칸만 표로 정리했습니다.
왕복 확인 예제는 `examples/model-worker/send_one.py`입니다.

| 파일 | 하는 일 |
|---|---|
| `sqs_bridge/__main__.py` | 워커 실행. 켠 큐마다 따로 돌림 |
| `sqs_bridge/worker.py` | 요청 하나 꺼내 처리하고 답 넣기 |
| `sqs_bridge/jeff.py` · `slm.py` | 같은 PC의 jeff-serve · vLLM에 묻기 |
| `sqs_bridge/client.py` | AI 서버 쪽: 요청 넣고 결과 큐에서 자기 답 받기 |
| `sqs_bridge/messages.py` | 서버·워커가 주고받는 편지 양식 (양쪽이 같아야 함) |
| `tests/` | 가짜 큐로 도는 테스트 (`python -m pytest -q`) |
