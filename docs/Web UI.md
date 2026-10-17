# Web UI

`tools/Web UI/` 설명서입니다. 빠른 사용법은 [`tools/Web UI/README.md`](../tools/Web%20UI/README.md)에 있습니다.

## 1. 개요

"상황 + 선택지 → 선택지별 확률"을 내는 판단 모델을 브라우저에서 다루는 개발 도구입니다.
지금 붙어 있는 모델은 [Jeff](https://github.com/firelex/jeff)(Qwen3.5-0.8B 기반, LoRA 어댑터 학습)입니다.

| 탭 | 하는 일 |
|---|---|
| 데이터셋 | `state`·`label`만 있는 .jsonl → 빈 칸 채우기 → 형식 검사 → family 단위 분할 → 누수 검사·지름길 보고서 → 시험셋 잠금 |
| 학습 | 데이터셋 고르고 학습 시작/멈추기 (설정은 '고급 설정'), loss·개발셋 정확도·nll 그래프. 끝나면 결과 어댑터가 서버에 자동 등록 |
| 평가 | 학습 전·후 모델이 시험셋을 풀어 "N개 중 a개 → b개" 판정, 점수는 시험셋별 기록 |
| 추론 테스트 | 입력 하나를 A(학습 전)·B(학습한 모델)가 나란히 판단. 선택지 설명 고쳐 다시 판단, 결과 기록 |

맨 위 진행 단계(① → ④)와 탭마다 "다음 →" 버튼이 있어서, 처음 쓰는 사람도 버튼만 따라가면 끝까지 갑니다.

구성은 세 부분입니다.

| 부분 | 폴더 | 내용 |
|---|---|---|
| 화면 | `loop/` | 탭과 흐름. 특정 프로젝트·모델 이름을 모름 |
| 모델 | `engines/jeff/` | Jeff 서버 연결·학습 실행. 다른 모델은 `engines/<이름>/`으로 추가 |
| 프로젝트 | `profiles/<이름>/` | 라벨·선택지 설명·예시. `example`(고객 문의 → 담당 팀), `fintris`(거래 처리 방식) |

## 2. 설치 방법

**필요한 것:** Windows 10/11, WSL2(Ubuntu 24.04), NVIDIA GPU(8GB면 충분, RTX 3070에서 확인), 디스크 약 10GB.
웹 UI는 Windows에서, Jeff 서버와 학습은 WSL2 안에서 돕니다(Jeff 학습이 리눅스 전용 기능을 써서). WSL2는 127.0.0.1 포트를 Windows와 같이 써서 주소는 그대로입니다.

### 2-1. 웹 UI (Windows, Python 3.11)

```bash
cd "tools/Web UI"
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
```

### 2-2. Jeff (WSL2 Ubuntu 24.04)

```bash
sudo apt-get install -y git build-essential python3.12-dev   # python3.12-dev 없으면 가속 부품이 CPU로 떨어짐
curl -LsSf https://astral.sh/uv/install.sh | sh
mkdir -p ~/jeff-train && cd ~/jeff-train
git clone https://github.com/firelex/jeff jeff
cd jeff && uv sync --extra lora --extra cuda
```

### 2-3. 모델 받기

베이스 모델은 저장소에 없습니다. Hugging Face [`mstrasser/jeff-base`](https://huggingface.co/mstrasser/jeff-base)의 `v1.3`을 받아 `~/jeff-train/models/jeff-base-v1.3`에 둡니다(약 1.7GB).

```bash
cd ~/jeff-train/jeff
uv run hf download mstrasser/jeff-base --revision v1.3 --local-dir ~/jeff-train/models/jeff-base-v1.3
```

WSL 안 폴더 구조 (`config/settings.yaml`의 `engines.jeff.train.root`로 바꿀 수 있음):

```
~/jeff-train/
  jeff/              Jeff 코드 + .venv
  models/            베이스 모델 (jeff-base-v1.3)
  runs/<이름>/       학습 기록 (학습 탭이 만듦)
  adapters/<이름>/   학습 결과 LoRA 어댑터 (서버가 읽음)
```

## 3. 사용 방법

### 켜기

가장 쉬운 방법은 `tools/Web UI/webui.bat` 더블클릭입니다 (처음이면 웹 UI 환경 설치 → Jeff 서버 켜기 → 웹 UI 켜기, 콘솔 창을 닫으면 둘 다 꺼짐). 따로 켜려면:

```bash
cd "tools/Web UI"
wsl -d Ubuntu-24.04 -u user -- bash engines/jeff/wsl/serve.sh   # Jeff 서버, 127.0.0.1:8765
.venv/Scripts/python loop/main.py                               # 웹 UI, http://127.0.0.1:7860
```

`-u user`는 WSL 계정 이름입니다. 다르면 `config/settings.yaml`의 `engines.jeff.train.user`와 같이 바꿉니다.

### 설정과 환경변수

설정 파일은 `config/settings.yaml`이고, 환경변수가 있으면 환경변수가 먼저입니다. 내 컴퓨터에서만 바꿀 값은 `config/settings.local.yaml`(저장소에 안 올림)에 씁니다.

| 설정 | 환경변수 | 기본값 | 뜻 |
|---|---|---|---|
| `profile` | `WEBUI_PROFILE` | `example` | 쓸 프로젝트 (`profiles/<이름>/`) |
| `engine` | `WEBUI_ENGINE` | `jeff` | 쓸 모델 (`engines/<이름>/`) |
| `engines.jeff.url` | `JEFF_URL` | `http://127.0.0.1:8765` | Jeff 서버 주소. 다른 GPU 서버로 옮길 땐 이것만 바꾸면 추론·평가는 그대로 |
| `engines.jeff.model` | `JEFF_MODEL` | 빈칸 | 기본 모델 이름. 비우면 서버가 알려주는 이름 |
| `engines.jeff.timeout` | | `30` | 요청 제한 시간(초) |
| | `WEBUI_NO_BROWSER` | | `1`이면 켤 때 브라우저를 새로 안 띄움 |

### 탭별로

- **데이터셋:** 파일 넣기 → "불러오기·검사" → "나누기·검사·잠그기". 결과는 `profiles/<이름>/data/<데이터셋>/`(train·development·calibration·test + `lock.json`). 누수 검사에 실패하거나 잠근 뒤 파일이 바뀐 데이터셋은 학습 탭에서 막힙니다.
- **학습:** 데이터셋을 고르고 "학습 시작" (방금 만든 데이터셋이 자동으로 골라짐). 이름을 비우면 `<데이터셋>-<시각>`으로 자동. 끝나면 그 이름으로 평가·추론 탭 모델 목록에 나옵니다. 한 번에 하나만 돌아갑니다.
- **평가:** "평가 실행" (기본 = 마지막 학습에 쓴 데이터셋의 시험셋 + 학습 전·마지막 학습 모델). 기록은 `profiles/<이름>/outputs/evaluations.jsonl`에 쌓이고, 같은 시험셋으로 3번 이상 평가하면 오염 경고가 뜹니다.
- **추론 테스트:** 예시를 고르거나 입력 JSON을 쓰고 "판단하기". A(학습 전)·B(마지막 학습 모델)가 나란히 판단합니다. 선택지 설명을 고치고 "설명 저장"을 누르면 `profiles/<이름>/prompt.<언어>.yaml`에 저장됩니다.

### 새 프로젝트 올리기

`profiles/example/`을 복사해 `profiles/<새 이름>/`을 만들고 `labels.yaml`(선택지), `prompt.en.yaml`·`prompt.ko.yaml`(지시문·선택지 설명), `examples.yaml`(추론 탭 예시), `profile.yaml`(이름·표시할 칸)을 고친 뒤 `WEBUI_PROFILE=<새 이름>`으로 켭니다.

## 4. 입출력

### 추론 (한 건)

입력: 상황(`state`, 아무 JSON 객체) + 선택지(`labels.yaml`의 id와 `prompt.*.yaml`의 설명).

```json
{"company": "Northwind Shoes", "channel": "email",
 "message": "My card shows two payments for the same order. Please send one back."}
```

출력: 고른 선택지, 선택지별 확률(합 1), 확신도, 응답 시간. (아래 숫자는 형식을 보여 주는 예시 값)

```json
{"choice": "refund", "probabilities": {"refund": 0.91, "delivery": 0.03, "account": 0.02, "other": 0.04},
 "confidence": 0.91, "elapsed_ms": 84}
```

### 데이터셋 탭에 넣는 파일

한 줄이 한 건이고, `state`와 `label`만 있으면 됩니다. 나머지(`id`·`family`·`question` 등)는 프로필에서 채웁니다. 정답(`label`)이 없는 줄은 받지 않습니다.

```json
{"state": {"company": "Copperline Bikes", "channel": "chat", "message": "I was charged twice ..."}, "label": "refund"}
```

### 학습 데이터 (데이터셋 탭이 만드는 것)

데이터 폴더에 `train.jsonl`, `development.jsonl`, `calibration.jsonl` 세 파일이 있어야 합니다. 한 줄이 한 건입니다.

```json
{"id": "c02-1", "family": "company-02",
 "state": {"company": "Copperline Bikes", "channel": "chat", "message": "I was charged twice ..."},
 "question": {"type": "choice", "instructions": "Which team should handle this message?",
              "criteria": {"refund": "Refunds, double charges and money back", "delivery": "...", "account": "...", "other": "..."}},
 "label": "refund"}
```

- `family`: 비슷한 건끼리 묶는 이름(예: 같은 회사·같은 가맹점). 같은 family가 학습과 시험에 나뉘어 들어가면 점수가 부풀려지므로, 나눌 때 family 단위로 나눕니다. Jeff 학습이 세 파일 사이 family·id 겹침을 검사합니다.
- 결과: WSL 안 `~/jeff-train/adapters/<이름>/`에 LoRA 어댑터(약 40MB). 학습 기록은 `~/jeff-train/runs/<이름>/`.

### 평가 (시험셋)

`test.jsonl`, 형식은 학습 데이터와 같습니다(`state`, `question.criteria`, `label` 필수). 출력은 한 문장 판정("처음 보는 문제 N개 중 a개 → b개")과 모델별 정확도·nll(낮을수록 좋음)·평균 확신도·평균 응답 시간 기록입니다. 점수는 같은 시험셋끼리만 비교합니다.

## 5. 주의 사항

- **외부 API 금지:** 데이터를 OpenAI·Gemini 등 외부로 보내지 않습니다. 모든 계산은 내 컴퓨터 안에서 합니다.
- **시험셋 분리:** `test.jsonl`은 학습 탭이 읽지도 복사하지도 않습니다. 시험셋으로 설명을 고치거나 학습 설정을 고르면 점수가 거짓이 됩니다.
- **결과가 매번 조금 다름:** 같은 설정으로 다시 학습해도 GPU 계산 순서 때문에 결과가 조금씩 다릅니다. 설정을 비교할 땐 여러 번 학습해 평균으로 봅니다.
- **학습은 지금 WSL 전용:** 학습 실행은 `engines/jeff/trainer.py`가 `wsl.exe`로 부릅니다. 추론·평가는 서버 주소만 바꾸면 다른 GPU 서버에서도 됩니다.
- **올리지 않는 것:** 모델 가중치, 학습 데이터·결과, `.venv`, 로그, `config/settings.local.yaml`.
- **접속 범위:** 웹 UI·Jeff 서버는 127.0.0.1에만 띄웁니다. 다른 컴퓨터에서 볼 땐 포트를 열지 말고 SSH 터널이나 Tailscale을 씁니다.
- **데이터가 적으면:** 지름길(정답과 상관없는 겉모양, 예: 요일 단어)을 배우고 과하게 자신만만해집니다. 데이터셋 탭의 지름길 보고서를 보고, 양과 다양성을 늘리세요.
- **라벨:** Fintris 프로필은 BE 기능명세서 F-20 기준 `slm`(sLM이 분류)·`ask`(사용자 질문). 팀 결정에 따라 바뀔 수 있습니다.
