# Web UI

판단 모델(지금은 [Jeff](https://github.com/firelex/jeff))을 브라우저에서 **데이터셋 → 학습 → 평가 → 추론 테스트**까지 다루는 개발 도구입니다.
"상황 + 선택지 → 선택지별 확률"을 내는 모델을 버튼으로 학습하고, 학습 전·후 모델을 같은 화면에서 비교합니다.

- 자세한 설명서(설치·입출력·주의 사항): [`docs/Web UI.md`](../../docs/Web%20UI.md)
- 예제 코드: [`examples/Web UI/`](../../examples/Web%20UI/)

## 화면

| 탭 | 하는 일 |
|---|---|
| 데이터셋 | `state`(내용)·`label`(정답)만 있는 .jsonl → 나머지 칸 자동 채움 → 형식 검사 → 묶음(family) 단위로 학습·개발·보정·시험 파일 분할 → 누수 검사·지름길 보고서 → 시험셋 잠금 |
| 학습 | 데이터셋 고르고 "학습 시작" (이름·에폭 등은 '고급 설정'). loss·개발셋 정확도·nll 그래프가 2초마다 갱신되고, 끝나면 결과 모델이 서버에 자동 등록 |
| 평가 | "평가 실행" → 학습 전·후 모델이 시험셋을 풀어 "N개 중 a개 → b개" 판정. 점수는 시험셋별 기록으로 쌓임 |
| 추론 테스트 | 입력 하나를 A(학습 전)·B(학습한 모델)가 나란히 판단. 선택지 설명을 고쳐 바로 다시 판단, 결과 기록 |

맨 위 상태줄은 5초마다 모델 서버 상태를 보여 줍니다: 🟢 준비됨 / 🟡 불러오는 중 / 🔴 꺼짐(켜는 명령이 같이 나옴).
그 아래 진행 단계(① 데이터셋 → ② 학습 → ③ 평가 → ④ 추론)가 어디까지 했는지 보여 주고, 탭마다 일이 끝나면 "다음 →" 버튼이 나옵니다.

## 실행 환경

- Windows + WSL2(Ubuntu 24.04) + NVIDIA GPU (RTX 3070 8GB에서 확인)
- 웹 UI는 Windows(Python 3.11), Jeff 서버와 학습은 WSL2 안에서 돕니다. Jeff 학습이 리눅스 전용 기능을 써서 Windows에서 바로 안 돌기 때문입니다.
- 의존성: 웹 UI는 `requirements.txt`(gradio 6.30, pyyaml, pytest). torch 등 모델 패키지는 WSL 안 Jeff 환경에만 설치합니다.

## 사용 방법

처음 설치는 [설명서의 설치 방법](../../docs/Web%20UI.md#2-설치-방법)을 따르세요.

**가장 쉬운 방법:** `webui.bat` 더블클릭 (PowerShell 이면 `.\webui.bat`). 처음이면 웹 UI 환경을 설치하고, Jeff 서버(WSL)와 웹 UI를 같이 켭니다. 콘솔 창을 닫으면 둘 다 꺼집니다.

따로 켜려면 아래 두 개를 켭니다.

```bash
cd "tools/Web UI"

# 1) Jeff 서버 켜기 (WSL 안에서 돎, 주소 127.0.0.1:8765)
wsl -d Ubuntu-24.04 -u user -- bash engines/jeff/wsl/serve.sh

# 2) 다른 창에서 웹 UI 켜기 → http://127.0.0.1:7860 (브라우저가 자동으로 열림)
.venv/Scripts/python loop/main.py
```

- Fintris 라벨로 켜기: `WEBUI_PROFILE=fintris .venv/Scripts/python loop/main.py` (기본은 예시 프로필 `example`)
- 다시 켤 때 새 창이 뜨지 않게: `WEBUI_NO_BROWSER=1`을 붙이고, 열려 있는 창에서 F5
- 테스트: `.venv/Scripts/python -m pytest` (항상 `example` 프로필로 돎)

## 폴더

```
loop/                 화면과 흐름 (main.py = 진입점, tabs/<탭>/ = 탭별 화면·로직)
engines/jeff/         Jeff 서버 연결(client.py) · WSL 학습 실행(trainer.py) · WSL 스크립트(wsl/)
profiles/             프로젝트별 라벨·설명·예시 (example = 고객 문의 → 담당 팀, fintris = 거래 처리 방식 slm·ask)
config/settings.yaml  화면 이름·프로필·서버 주소 등 설정
tests/                자동 테스트
```

## 주의

- 데이터를 외부 API(OpenAI·Gemini 등)로 보내지 않습니다.
- 시험셋(`test.jsonl`)은 학습에 쓰지 않습니다. 시험셋을 읽는 곳은 평가 탭뿐입니다.
- 모델 가중치·학습 데이터·학습 결과는 저장소에 올리지 않습니다. 받는 곳은 설명서에 있습니다.
- 웹 UI·Jeff 서버는 127.0.0.1에만 띄웁니다. 다른 컴퓨터에서 볼 땐 포트를 열지 말고 SSH 터널이나 Tailscale을 씁니다.
