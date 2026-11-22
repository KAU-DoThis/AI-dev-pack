# model-worker

코드 위치: `modules/model-worker/` · 예제: `examples/model-worker/`

## 1. 개요

AI 서버는 Jeff·sLM을 직접 부르지 않습니다. **요청 큐에 넣고, 결과 큐에서 답을 받습니다.**
모델이 있는 PC에서는 워커가 돌면서 자기 요청 큐를 꺼내 같은 PC의 모델에 묻고, 답을 결과 큐에 넣습니다.

```
AI 서버 ──넣기──▶ fintris-dev-jeff-requests ──꺼내기──▶ 워커(Jeff PC) ──127.0.0.1──▶ jeff-serve
AI 서버 ──넣기──▶ fintris-dev-slm-requests  ──꺼내기──▶ 워커(sLM PC)  ──127.0.0.1──▶ vLLM
AI 서버 ◀─꺼내기── fintris-dev-agent-replies ◀──넣기─── 두 워커 다
```

- 모델 PC는 포트를 열지 않습니다. VPN도 필요 없습니다. 워커는 AWS로 나가는 연결만 씁니다.
- 워커 코드는 하나입니다. PC마다 `.env`만 다르게 씁니다.
- 이 폴더에는 두 부분이 들어 있습니다.
  - **워커** (`python -m sqs_bridge`): 모델 PC에서 실행합니다.
  - **클라이언트** (`sqs_bridge/client.py`의 `ModelQueue`): AI 서버가 불러 씁니다 (3.4절).

| PC | 켜는 일 | 꺼내는 큐 | 묻는 모델 (같은 PC) | AWS 프로필 |
|---|---|---|---|---|
| 재혁 PC | `JEFF_ENABLED=true` | `fintris-dev-jeff-requests` | jeff-serve | `fintris-jeff` |
| 승현 PC | `SLM_ENABLED=true` | `fintris-dev-slm-requests` | vLLM | `fintris-slm` |

한 PC에 두 모델이 다 있으면 둘 다 `true`로 켜도 됩니다. 큐마다 따로 돌아서 한쪽이 막혀도 다른 쪽은 계속 처리합니다.

아래 설명은 **Jeff로 처음부터 끝까지** 보여 줍니다. sLM도 흐름은 똑같고, 다른 칸은 3.3절과 4.2절에 모았습니다.

## 2. 설치 방법

1. **Python 3.11 이상.** `modules/model-worker/`에서:
   ```
   pip install -r requirements.txt
   ```
2. **AWS 키 등록.** 재혁에게 받은 이 PC 전용 키를 프로필로 등록합니다. 키는 개인톡으로만 주고받습니다.
   ```
   aws configure --profile fintris-jeff
   ```
   - Access Key ID·Secret Access Key: 받은 값
   - Default region: `ap-northeast-2`
   - Output format: 비워 두기

   이 키로 할 수 있는 건 **자기 요청 큐 꺼내기·지우기 + 결과 큐 넣기**뿐입니다. 다른 큐는 거부됩니다.
   확인: `aws sts get-caller-identity --profile fintris-jeff`
3. **모델 서버를 먼저 켭니다.** Jeff는 jeff-serve입니다. `tools/Web UI/`에서 (`docs/Web UI.md`와 같은 방법):
   ```
   wsl -d Ubuntu-24.04 -u user -- bash engines/jeff/wsl/serve.sh
   ```
   `http://127.0.0.1:8765/health`가 열리면 됩니다.

## 3. 사용 방법

### 3.1 `.env` 채우기 (Jeff)

```
cp .env.example .env          # Windows PowerShell: Copy-Item .env.example .env
```

`.env`는 `modules/model-worker/.env`에 둡니다. 깃에 올라가지 않습니다 (`.gitignore`).

| 이름 | 필수 | 넣을 값 | 예시 |
|---|---|---|---|
| `AWS_PROFILE` | O | 2절에서 등록한 프로필 | `fintris-jeff` |
| `AWS_REGION` | | 리전 | `ap-northeast-2` (기본값) |
| `JEFF_ENABLED` | | 이 PC에서 Jeff를 켜면 `true` | `true` |
| `SLM_ENABLED` | | 이 PC에서 sLM을 켜면 `true` | `false` |
| `JEFF_REQUESTS_URL` | O | Jeff 요청 큐 주소 전체 (재혁에게 받음) | `https://sqs.ap-northeast-2.amazonaws.com/<계정번호>/fintris-dev-jeff-requests` |
| `JEFF_URL` | | jeff-serve 주소 | `http://127.0.0.1:8765` (기본값) |
| `JEFF_MODEL` | | jeff-serve에서 쓸 모델 이름. 비우면 jeff-serve 기본 모델 | (비움) |
| `JEFF_PROMPT_FILE` | | Jeff에 같이 보낼 선택지(`slm`·`ask`)·지시문 파일. 비우면 요청에 선택지가 꼭 있어야 함 | `../../tools/Web UI/profiles/fintris/prompt.ko.yaml` |
| `JEFF_TIMEOUT` | | 모델 답을 기다리는 최대 시간(초). 서버가 기다려 주는 시간이 더 짧으면 그만큼만 기다립니다 | `30` |

**필수** 칸이 비어 있으면 워커가 시작할 때 `설정 없음: <이름>`을 띄우고 멈춥니다.

### 3.2 실행·멈추기

`modules/model-worker/`에서:
```
python -m sqs_bridge
```
정상이면 이렇게 뜨고 기다립니다.
```
jeff 큐 확인됨
[jeff] 대기 중: https://sqs.../fintris-dev-jeff-requests (일: jeff)
```
요청을 하나 처리할 때마다 한 줄이 찍힙니다.
```
[jeff] 답장: 3f9a… task=jeff ok=True 95ms
```
11/18 실제 큐로 잰 왕복 시간(AI 서버가 넣고 답을 받기까지): 첫 요청 약 1.8초, 그다음부터 약 0.1초.

- 멈추기: Ctrl+C. 처리 중인 요청을 끝내고 최대 20초 안에 멈춥니다.
- 한 줄이라도 오류로 죽으면 전체가 멈추고 종료 코드 1로 끝납니다. 다시 띄우면 됩니다. 처리 못 한 요청은 큐에 남아 있습니다.

### 3.3 sLM으로 바꾸기

흐름(꺼내기 → 기한 확인 → 같은 PC 모델에 묻기 → 결과 큐에 넣기)과 실행 명령은 Jeff와 같습니다. 아래 칸만 다릅니다.

| | Jeff | sLM |
|---|---|---|
| 켜기 | `JEFF_ENABLED=true` | `SLM_ENABLED=true` |
| AWS 프로필 | `fintris-jeff` | `fintris-slm` |
| 요청 큐 칸 | `JEFF_REQUESTS_URL` | `SLM_REQUESTS_URL` (`…/fintris-dev-slm-requests`) |
| 모델 서버 | jeff-serve | vLLM을 **OpenAI 호환 서버**로 띄움 |
| 모델 주소 칸 | `JEFF_URL` | `SLM_URL` — **`/v1`까지만** (예: `http://127.0.0.1:8000/v1`). 뒤의 `/chat/completions`는 워커가 붙임 |
| 모델 이름 칸 | `JEFF_MODEL` | `SLM_MODEL` (필수) — `curl http://127.0.0.1:8000/v1/models` 응답의 `id` 그대로 |
| 키 칸 | 없음 | `SLM_API_KEY` — vLLM을 `--api-key`로 띄웠을 때만, 아니면 비움 |
| 지시문 | `JEFF_PROMPT_FILE` | `SLM_PROMPT` (`v1`~`v4`, 기본 `v4`, `sqs_bridge/slm.py`에 있음) |

주고받는 데이터가 어떻게 다른지는 4.2절에 있습니다.

### 3.4 AI 서버에서 부르기 (클라이언트)

AI 서버는 `sqs_bridge/client.py`의 `ModelQueue`로 부릅니다. AI 레포에는 아직 옛 버전이 들어가 있고, 이 버전으로 바꾸는 건 나중에 붙입니다.

```python
from sqs_bridge import ModelQueue, ModelTimeout

# 프로세스 하나에 ModelQueue 하나만. jeff·slm 둘 다 이 하나로 부릅니다.
q = ModelQueue(boto3.client("sqs"), {"jeff": JEFF_REQUESTS_URL, "slm": SLM_REQUESTS_URL}, MODEL_REPLIES_URL)
q.start()      # 서버 켤 때 (안 불러도 첫 call()이 띄움)
try:
    res = q.call("jeff", {"state": {...}}, timeout=10)
except ModelTimeout:
    ...        # 사용자에게 묻기
q.stop()       # 서버 끌 때
```

결과 큐에서 답을 찾는 방식:
1. `call()`은 요청 ID를 **대기 목록에 먼저 올리고** 요청 큐에 보낸 뒤, 자기 답만 기다립니다.
2. 결과 큐는 서버 안의 스레드 하나만 읽습니다. 꺼낸 답의 요청 ID가 대기 목록에 있으면 그 요청에 넘기고 큐에서 지웁니다.
3. 대기 목록에 없는 답(시간 초과로 이미 포기한 요청 등)은 지웁니다.
4. `retryable: true` 실패는 같은 요청 ID·같은 기한으로 한 번 더 보냅니다.
5. 기한은 대기 목록에서만 봅니다. 시간이 지나면 목록에서 빼고 `ModelTimeout`을 냅니다.

EC2에서는 키 없이 서버 역할 권한으로 동작합니다.

### 3.5 왕복 확인 예제

워커를 띄운 뒤, 요청 하나를 넣고 답이 돌아오는지 `examples/model-worker/send_one.py`로 확인할 수 있습니다.

```
python examples/model-worker/send_one.py jeff 명동칼국수
python examples/model-worker/send_one.py slm "스타벅스 강남점"
```

이 예제는 AI 서버 역할을 하므로 **서버 쪽 권한**(요청 큐 넣기 + 결과 큐 꺼내기·지우기)이 있는 프로필이 필요합니다. 워커용 키(`fintris-jeff`·`fintris-slm`)로는 거부됩니다. 자세한 준비는 예제 파일 맨 위에 있습니다.

### 3.6 테스트

`modules/model-worker/`에서 (가짜 큐로 돌아서 AWS·모델 없이 됩니다):
```
pip install pytest
python -m pytest -q
```

## 4. 입출력

워커가 큐 편지를 열고 싸는 건 알아서 합니다. 모델 쪽은 4.1의 (2)·(3) 형식만 맞으면 됩니다.

### 4.1 Jeff

**(1) Jeff 요청 큐에서 꺼내는 요청**
```json
{"task": "jeff",
 "payload": {"state": {"merchant_or_payee": "명동칼국수", "amount": 9000, "direction": "OUT",
                       "account_type": "CARD", "occurred_at": "2026-11-18T12:30"}},
 "reply_to": "https://sqs.../fintris-dev-agent-replies", "request_id": "3f9a…", "expires_at": 1763380000.0}
```
`expires_at`(기한)이 지난 요청은 모델에 묻지 않고 버립니다. AI 서버가 이미 기다리기를 그만둔 요청이기 때문입니다.

**(2) 워커 → jeff-serve** `POST {JEFF_URL}/v1/systemone`
```json
{"model": "<JEFF_MODEL>", "state": {…(1)의 state 그대로…},
 "questions": {"choice": {"type": "choice", "criteria": {"slm": "…", "ask": "…"}, "instructions": "…"}}}
```
선택지·지시문은 `JEFF_PROMPT_FILE`에서 읽습니다.

**(3) jeff-serve → 워커**
```json
{"answers": {"choice": {"choice": "slm", "probabilities": {"slm": 0.83, "ask": 0.17}, "confidence": 0.83}}}
```

**(4) 결과 큐에 넣는 답**
```json
{"request_id": "3f9a…", "ok": true,
 "result": {"choice": "slm", "probabilities": {"slm": 0.83, "ask": 0.17}, "confidence": 0.83, "model": "<jeff-serve 모델 이름>"},
 "error": null, "expires_at": 1763380000.0, "retryable": false}
```
모델 호출이 실패하면 `ok: false`와 `error`를 넣어 바로 알립니다.
모델 서버에 못 닿은 실패(꺼짐·연결 끊김·시간 초과)면 `retryable: true`를 넣습니다. 그러면 AI 서버가 기한 안에서 한 번 더 보냅니다.
그 밖의 실패(설정·형식 오류 등)는 다시 보내도 같으므로 `retryable: false`입니다.

### 4.2 sLM (Jeff와 다른 것만)

| | Jeff | sLM |
|---|---|---|
| 요청 payload | `{"state": {…}}` | `{"name": "스타벅스 강남점", "merchant_type": "커피전문점"}` (`merchant_type`은 없을 수 있음. 금액·시간·사용자 ID는 안 보냄) |
| 모델에 보내는 것 | `POST /v1/systemone` | `POST {SLM_URL}/chat/completions` (OpenAI 표준, `temperature: 0`) |
| 모델 답에서 꺼내는 것 | `choice`·`probabilities` | 답 글 안의 `"category_code":"<코드>"` (앞뒤에 다른 글이 있어도 이 부분만 뽑음) |
| 결과 `result` | `{"choice", "probabilities", "confidence", "model"}` | `{"category_code": "CAFE", "prompt": "v4"}` |

sLM 카테고리 코드: `FOOD` `DELIVERY_DINING` `CAFE` `TRANSPORT` `HOUSING` `TELECOM` `LIVING` `SHOPPING` `LEISURE` `HEALTH` `SUBSCRIPTION` `ETC` `UNKNOWN`.
답 글에서 코드를 못 찾으면 `category_code`가 `null`입니다. `null`·`UNKNOWN`·목록 밖의 코드면 AI 서버가 사용자에게 묻는 쪽으로 넘깁니다.

## 5. 주의 사항

- **편지 양식(`sqs_bridge/messages.py`)은 AI 서버와 같아야 합니다.** 한쪽만 고치면 서로 못 읽습니다. 고칠 때는 재혁·승현이 같이 정합니다.
- **같은 결과 큐를 읽는 `ModelQueue`를 둘 만들면 안 됩니다.** 서로의 답을 "대기 목록에 없음"으로 지웁니다. 한 서버 안에서는 하나를 같이 씁니다.
- **모델을 바꾸면** 정확도와 답 형식을 다시 잽니다.
- **학교 Qwen 같은 외부 서버에는 실제 거래를 보내지 않습니다.** 실제 거래는 우리 모델로만 보냅니다.
- 키는 깃·노션·단톡에 올리지 않습니다. `.env`는 깃에 올라가지 않습니다.

### 자주 나는 오류

| 화면 | 원인 | 할 일 |
|---|---|---|
| `설정 없음: JEFF_REQUESTS_URL` 등 | `.env` 필수 칸이 비었음 | 3.1절·3.3절 필수 칸 채우기 |
| `켠 일이 없음` | `JEFF_ENABLED`·`SLM_ENABLED` 둘 다 `false` | 이 PC 것을 `true`로 |
| `jeff 큐에 접근 못 함` / `slm 큐에 접근 못 함` | 키 등록 안 됨, `AWS_PROFILE` 오타, 큐 주소 오타, 다른 PC 키를 씀 | `aws sts get-caller-identity --profile <프로필>` → 큐 주소 확인 → 안 되면 재혁에게 |
| `ok=False (ModelUnavailable: jeff-serve 연결 실패 …)` | jeff-serve가 꺼졌거나 `JEFF_URL`이 틀림 | `http://127.0.0.1:8765/health` 확인 |
| `ok=False (ModelUnavailable: sLM 연결 실패 …)` | vLLM이 꺼졌거나 `SLM_URL`이 틀림 | `curl http://127.0.0.1:8000/v1/models` 확인 |
| `ok=False (RuntimeError: sLM 404)` | `SLM_URL`에 `/chat/completions`까지 넣었거나 `/v1`이 빠짐 | `…/v1`로 고치기 |
| `ok=False (RuntimeError: sLM 401)` | vLLM에 키를 걸었는데 `SLM_API_KEY`가 비었거나 다름 | 키 맞추기 |
| `ok=False (ValueError: 선택지(options)가 없음 …)` | `JEFF_PROMPT_FILE`이 비었음 | 3.1절 경로 넣기 |
| 시작하자마자 `FileNotFoundError` | `JEFF_PROMPT_FILE` 경로가 틀림 (`modules/model-worker/` 기준 상대 경로) | 경로 확인 |
| 답장 줄이 안 찍힘 | 큐에 요청이 없음 (정상) | AI 서버나 3.5절 예제로 요청을 넣어야 찍힘 |
