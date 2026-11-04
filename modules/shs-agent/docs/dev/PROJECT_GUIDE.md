# shs-agent 진행 현황과 코드 구조

> 기준일: **2026-10-20 KST**  
> 대상: 다음 개발 작업에 들어가기 전에 프로젝트의 현재 상태와 실행 구조를 파악하려는 개발자  
> 기준: 현재 작업 디렉토리의 코드, 테스트, 1–8단계 구현 보고서  
> 읽기 버전: [HTML](PROJECT_GUIDE.html) · 상세 사용법: [Host 사용 계약](HOST_USAGE.md)

**현재는 8단계인 ‘전체 실행 통합과 Host 계약 검증’까지 구현되어 있다.** 단일 작업 실행, 계획, 재귀 Sub-agent, 부모·자식 통신, Host의 도구 승인, 실행 추적과 JSON 재조회가 하나의 실행 경로로 연결되어 있다. 이제 주요 관심사는 기능을 처음 만드는 것보다, 실제 모델이 복잡한 업무에서 이 기능들을 제대로 사용하도록 품질을 높이고 Host에 연결하는 것이다.

다만 **엔진이 계약대로 작동한다는 것과 모델이 업무를 정확히 완수한다는 것은 별개의 결과**다. Fake Provider로 기능을 검증한 범위는 넓지만, 실제 모델 검증은 작은 작업의 성공과 복잡한 작업의 실패가 함께 남아 있다.

## 목차

1. [지금 어디까지 했나](#progress)
2. [프로젝트가 맡는 일](#boundary)
3. [디렉토리 지도](#layout)
4. [핵심 부품과 연결 관계](#components)
5. [작업 한 번의 실행 흐름](#execution)
6. [Context·Conversation·Memo·Plan의 차이](#context)
7. [Sub-agent와 통신 구조](#subagents)
8. [도구 실행과 Host 승인](#tools)
9. [상태·이벤트·Tracer·결과 읽기](#observation)
10. [실행해 보면서 코드 읽기](#reading)
11. [남은 문제와 다음 작업 순서](#next)
12. [검증 근거와 참고 문서](#evidence)

<a id="progress"></a>
## 1. 지금 어디까지 했나

### 개발 단계

| 단계 | 구현된 내용 | 현재 의미 |
|---|---|---|
| 초기 Storage | Storage 계약, SQLAlchemy 기반 SQLite·PostgreSQL·MySQL 구현 | 저장소 모듈은 존재하지만 현재 Agent Runtime에 자동 연결되지는 않음 |
| 1단계 | Solver·Session·Runtime·Core 분리, 공개 실행 API, 작업 정보 관리 | 하나의 Task를 시작하고 제어하고 결과를 받는 기본 실행 구조 |
| 2단계 및 보완 | Tracer, 상세 Artifact, TaskState·AgentState, 상태 재조회 | 지금 무엇을 하는지와 왜 그렇게 되었는지를 구분해서 관찰 |
| 3단계 | Planning/Replanning | Task별 계획 생성·진행 갱신·수정 이력 관리 |
| 4단계 | 직접 Sub-agent 생성·대기·결과 회수·취소 | 부모와 자식이 독립된 작업 상태로 실행 |
| 5단계 | 부모·자식 질문/답변, 알림, 추가 지시 | 다른 Task의 정보를 기다리고 다음 판단에 반영 |
| 6단계 | 재귀 위임, Root 전체 생성 수·깊이 제한, 전체 TaskTree | 자식의 자식도 같은 엔진으로 실행 |
| 7단계 | 동적 도구 요청·전달·Host 승인/거부·적용 | 필요한 도구를 요청하되 실제 권한은 Host가 부여 |
| 8단계 | 전체 Host 예제, Live/Replay 대조, 업무 수용 평가, 배포 검증 | 위 기능들을 연결한 사용 흐름을 검증 |

마지막 구현 보고서는 **2026-10-18의 8단계 보고서**다. 그 단계는 기존 엔진을 활용한 예제·통합 테스트·문서·배포 구성을 보완했으며, 새 Runtime이나 Scheduler를 추가한 단계는 아니다.

### 검증 결과를 어떻게 읽어야 하나

| 구분 | 확인된 결과 | 해석 |
|---|---|---|
| 오프라인 회귀 | **2026-10-20 재실행: 240개 통과, 47.884초** | Fake Provider와 제어 가능한 도구로 엔진의 동작 계약 검증 |
| 전체 Fake 시나리오 | Root → A → A-1 및 B, 질문·계획 수정·도구 거부/승인·실제 handler 조회·결과 회수 | 4개 Task를 연결하여 정산 완료 3건·2,500원 계산 |
| 실제 모델의 작은 작업 | 10월 18일 `gemma4:e4b` 1회 실행에서 업무 수용 성공 | 깊이 1의 자식, Host 답변, 정적 도구 조회와 정답 확인 |
| 실제 모델의 복잡한 작업 | 7단계의 4개 모델 실험에서 업무 완수 **0/4** | 빈 응답·도구 인자 오류·Context 증가·Host 복구 대응 문제 미해결 |
| wheel/sdist | 8단계 당시 각각 240개 테스트 및 예제 실행 통과 기록 | 현재 문서 경로 변경 후 배포가 그대로 통과한다는 뜻은 아님 |

실제 작은 작업에서는 깊이 2 이상 재귀, Agent 간 질의, 동적 도구 승인, Replan, 실행 중 지시·취소를 관찰하지 않았다. 이 기능들의 Fake 검증 결과를 실제 모델 성공으로 합산하면 안 된다.

### Git에 저장된 상태와 작업 폴더의 차이

- 조회 시점의 최신 커밋은 **`12f9f89` — `add storage beta`**, 2026-10-15 23:53:43 KST다.
- 현재 동작을 구성하는 많은 변경과 새 파일은 아직 커밋되지 않았다. `docs/`, `tests/`, 새 하위 패키지와 예제가 여기에 포함된다.
- 따라서 이 문서는 **현재 작업 폴더 기준**이다. 커밋만 다시 체크아웃하면 여기 설명한 최신 구조와 같지 않다.
- 패키지 버전은 `0.1.0`이다. 목표 아키텍처 문서의 `v1.0`은 설계 문서 버전이다.

<a id="boundary"></a>
## 2. 프로젝트가 맡는 일

**shs-agent는 Host가 준 목표를 외부 LLM과 도구로 해결하는 Python 라이브러리다.** Host는 이 라이브러리를 사용하는 CLI, 웹 서버, 데스크톱 앱 또는 상위 Agent를 뜻한다.

| 주체 | 맡는 일 | 예 |
|---|---|---|
| Host | 목표, 모델 선택, 도구 구현, 접근 권한, 사용자 입력, 결과 검증, 보관 정책 | 어떤 DB를 읽게 할지, 추가 도구를 승인할지, 답이 맞는지 판단 |
| shs-agent | 실행 반복, Context 구성, 내부 도구, 계획, 위임, 통신, 상태와 기록 | LLM 판단 → 도구 호출 → 결과 반영 → 다음 판단 |
| LLM Provider | 공통 요청을 실제 모델 API로 변환하고 응답 반환 | Ollama·OpenAI adapter |
| 외부 handler | Host가 등록한 실제 작업 수행 | 허용한 파일 읽기, SQL 조회, 업무 API 호출 |

모델 가중치 로딩, GPU 운영, 사용자 UI, 파일/프로세스 격리, 장기 Memory 정책은 Host 또는 외부 시스템의 책임이다. 저장소에 `storage/`가 있어도 Task가 자동 저장되거나 중단 지점부터 재개되지는 않는다.

<a id="layout"></a>
## 3. 디렉토리 지도

아래는 현재 구조에서 먼저 알아야 할 파일들이다. `__init__.py` 등 일부 보조 파일은 생략했다.

```text
shs-agent/
├─ src/shs_agent/
│  ├─ __init__.py                 공개 API export, 패키지 버전
│  ├─ agent/                     작업 실행 엔진
│  │  ├─ solver.py               Task 조립과 공개 진입점
│  │  ├─ core.py                 LLM 판단 한 번
│  │  ├─ types.py                Request/Result/Limits/Status 계약
│  │  ├─ state.py                TaskState/AgentState/SessionState
│  │  ├─ _async.py               sync/async Provider·handler 연결
│  │  ├─ runtime/runner.py        반복 실행, 경계 제어, 종료
│  │  ├─ session/
│  │  │  ├─ handle.py            Host에 제공하는 TaskSession
│  │  │  └─ channel.py           상태, 명령, 질문, 이벤트 전달
│  │  ├─ context/
│  │  │  ├─ state.py             TaskContext와 읽기용 Snapshot
│  │  │  ├─ builder.py           LLM 입력 메시지 조합
│  │  │  ├─ conversation.py      원본 대화와 도구 결과 기록·조회
│  │  │  ├─ memo.py              Task별 작업 메모
│  │  │  └─ compaction.py        과거 대화 압축 범위와 요약 관리
│  │  ├─ planning/plan.py        계획 항목·의존성·revision 관리
│  │  ├─ subagent/
│  │  │  ├─ manager.py           Root 전체 관계·한도·Session 관리
│  │  │  ├─ scope.py             각 Agent의 직접 자식 관리 범위
│  │  │  └─ models.py            SubagentConfig, TaskTreeSnapshot
│  │  ├─ communication/
│  │  │  ├─ endpoint.py          부모/자식 통신 인터페이스
│  │  │  └─ models.py            메시지·요청 데이터
│  │  ├─ tools/
│  │  │  ├─ schemas.py           LLM에 보여 주는 내부 도구 정의
│  │  │  ├─ session_tools.py     내부 도구와 외부 도구 연결
│  │  │  └─ integration.py       도구 요청·Host 결정·적용 상태
│  │  ├─ tracer/
│  │  │  ├─ tracer.py            이벤트·Artifact 기록
│  │  │  ├─ models.py            기록 Snapshot, 조회와 JSON
│  │  │  └─ serialization.py     기록 가능한 값으로 변환
│  │  └─ _deprecated/            이전 구현 보관
│  ├─ llm/                       Provider 공통 계약과 adapter
│  │  ├─ types.py                LLMResponse, ToolCall, ChatOptions
│  │  ├─ base.py                 BaseLLM와 도구 스키마 변환
│  │  ├─ registry.py             Provider 등록
│  │  ├─ observation.py          공통/SDK 요청·응답 관측
│  │  └─ provider/               ollama.py, openai.py
│  ├─ tool/                      범용 외부 도구 부품
│  │  ├─ model.py                ToolSpec + handler = Tool
│  │  ├─ registry.py             등록·조회·스키마 목록
│  │  └─ executor.py             이름과 인자로 handler 호출
│  └─ storage/                   현재 실행 엔진과 별도인 저장 모듈
│     ├─ base.py, model.py       저장 계약과 Record
│     └─ provider/               공통 SQLAlchemy 구현과 DB별 연결
├─ examples/                     라이브러리를 사용하는 Host 예제
├─ tests/                        회귀 테스트와 별도 실모델 검증
├─ docs/dev/
│  ├─ PROJECT_GUIDE.md/html      이 문서
│  ├─ HOST_USAGE.md              공개 API와 Host 사용 계약
│  ├─ architecture/              목표 설계와 이전 버전
│  └─ reports/                   단계별 구현·실험 보고서
├─ build/                        로컬 실행 기록·검증 산출물
├─ dist/                         빌드된 배포물
├─ pyproject.toml                의존성·패키지·배포 설정
└─ MANIFEST.in                   sdist 포함/제외 규칙
```

예전의 `agent/context.py`, `runtime.py`, `session.py`는 현재 같은 이름의 **하위 패키지**로 나뉘었다. 이전 코드를 찾다가 `_deprecated/`를 현재 실행 코드로 읽지 않도록 주의한다. 해당 디렉토리는 현재 공개 실행 경로와 배포 대상에서 제외되어 있다.

<a id="components"></a>
## 4. 핵심 부품과 연결 관계

먼저 **Solver는 조립, Session은 Host 창구, Runtime은 실행 제어, Core는 LLM 판단 한 번**으로 기억하면 된다.

```text
Host
  │ Provider / model / tools / limits / TaskRequest
  ▼
ProblemSolver ── _assemble_task()로 Task마다 같은 부품 조립
  │
  ├─ TaskSession ◀──────── Host의 관찰·답변·지시·취소
  │      │
  │      └─ TaskChannel ── 현재 상태 + 명령/입력 대기열
  │              └─ Tracer ── Events + Artifacts
  │
  └─ TaskRuntime ───────── 실행 반복과 종료 판단
         ├─ TaskContext → ContextBuilder → LLM messages
         ├─ Planning → 현재 Plan과 변경 이력
         ├─ AgentCore → Provider.chat() → LLMResponse
         └─ SessionTools
                ├─ 내부 도구 → Context / Plan / 질문 / 위임
                └─ 외부 도구 → ToolExecutor → Host handler

SubagentManager → 자식 생성 → 동일한 _assemble_task() 경로
```

### 주요 파일별 책임

| 부품 | 실제로 하는 일 | 읽을 위치 |
|---|---|---|
| `ProblemSolver` | 입력 검증, 기본값 결정, Root/자식 공통 조립, `create_task/start/solve` | [solver.py](../../src/shs_agent/agent/solver.py) |
| `TaskSession` | 실행 시작, 결과 대기, 이벤트/상태/상세 조회, Host 명령 접수 | [handle.py](../../src/shs_agent/agent/session/handle.py) |
| `TaskRuntime` | 반복 판단, 한도 검사, 도구 호출 묶음 처리, 취소·추가 지시·종료 처리 | [runner.py](../../src/shs_agent/agent/runtime/runner.py) |
| `AgentCore` | Provider 호출, 입력 사본 전달, 응답 형식 검증과 공통 타입 정규화 | [core.py](../../src/shs_agent/agent/core.py) |
| `TaskChannel` | 현재 상태의 소유자, Host 질문과 명령, 통신 대기열, Tracer로 기록 전달 | [channel.py](../../src/shs_agent/agent/session/channel.py) |
| `TaskContext` / `ContextBuilder` | 작업 정보를 보유하는 역할 / 그 정보로 실제 LLM 입력을 만드는 역할 | [state.py](../../src/shs_agent/agent/context/state.py), [builder.py](../../src/shs_agent/agent/context/builder.py) |
| `SessionTools` | 현재 Task에 제공할 도구 목록 결정, 내부/외부 handler로 분기 | [session_tools.py](../../src/shs_agent/agent/tools/session_tools.py) |
| `SubagentManager` / `SubagentScope` | 전체 트리 관리 / 해당 Agent가 다룰 직접 자식 범위 제한 | [manager.py](../../src/shs_agent/agent/subagent/manager.py), [scope.py](../../src/shs_agent/agent/subagent/scope.py) |
| `Tracer` | 사건과 상세 자료를 보관하고 독립 Snapshot으로 반환 | [tracer.py](../../src/shs_agent/agent/tracer/tracer.py) |

`tool/`과 `agent/tools/`도 구분해야 한다. 전자는 범용 도구 등록·호출 부품이다. 후자는 `task_complete`, `plan_create`, `subagent_spawn`처럼 **Agent 실행 계약에 연결되는 도구**를 구현한다.

`llm/provider/`는 모델 API 차이를 흡수한다. Runtime은 Ollama SDK를 직접 다루지 않고 `AgentCore`의 공통 응답을 받는다. 새 Provider를 연결할 때 우선 확인할 계약은 `chat(model, messages, tools, options) → LLMResponse`다.

<a id="execution"></a>
## 5. 작업 한 번의 실행 흐름

### Host가 고르는 진입점

| API | 반환 | 사용하는 상황 |
|---|---|---|
| `solver.create_task(request=..., model=...)` | 아직 시작하지 않은 `TaskSession` | 관찰 경로 등을 준비한 뒤 직접 `await session.start()` |
| `await solver.start(task, model=...)` | 시작된 `TaskSession` | 실행 중 질문·승인·지시·취소가 필요한 Host |
| `await solver.solve(task, model=...)` | 최종 `TaskResult` | 결과가 나올 때까지 기다리는 간단한 호출 |

Provider는 `ProblemSolver(provider)`의 기본값 또는 작업별 `provider=`로 전달한다. 도구를 생략하면 Task마다 새 빈 외부 Registry를 만들고 내부 도구는 제공한다.

### Runtime의 한 바퀴

1. **입력 처리:** Host 명령과 Agent 메시지를 확인한다. 추가 지시, 취소, Host 질문의 응답 제한을 검사한다.
2. **실행 준비:** 단계 한도를 확인하고 승인 대기 중인 도구를 적용한다. 필요하면 오래된 Conversation을 LLM으로 요약한다.
3. **입력 고정:** 이번 호출에 제공할 도구 스키마와 instruction revision을 잡고 Context를 만든다.
4. **판단:** `AgentCore.step()`이 Provider를 호출한다. 요청·응답·시간·상태를 기록한다.
5. **응답 검증:** 새 지시로 오래된 판단이 되었는지, 취소되었는지, 빈 응답 또는 잘린 응답인지 확인한다.
6. **기록과 실행:** assistant 메시지·도구 호출을 Conversation에 기록하고 도구를 호출 순서대로 실행한다.
7. **다음 판단 또는 종료:** 도구 결과를 반영해 반복한다. 유효한 단독 `task_complete` 호출이면 결과를 확정한다.

`_loop()`가 큰 흐름, `_run_batch()`가 도구 묶음, `_finish()`가 최종 정리다. 현재 한 Task의 한 응답에 담긴 도구 호출은 순서대로 처리한다. 여러 Sub-agent Task는 `asyncio`로 동시 진행할 수 있지만 실제 LLM 서버의 추론 병렬성은 별도 문제다.

### 실행 중 지시와 취소

- **추가 지시:** revision이 바뀐다. 이미 진행 중인 LLM 호출이 돌아와도 이전 지시 기준 응답이면 버리고 새 판단으로 넘어간다.
- **도구 실행 중 지시:** 진행 중 handler의 결과는 보존하고, 아직 실행하지 않은 호출에는 이전 지시가 대체되었음을 반영한다. 대화와 tool-result 쌍을 정리한 경계에서 지시를 Context에 넣는다.
- **취소:** 협력적으로 처리한다. 진행 중 외부 LLM/handler가 끝나기를 기다릴 수 있다. Host 질문 대기는 해제한다.
- **부모의 실패·취소:** 실행 중인 자식에게 취소를 요청하고 결과 회수를 기다린다. 이 정리가 하위 깊이로 이어진다.

동기 Provider와 handler는 [_async.py](../../src/shs_agent/agent/_async.py)의 `invoke()`가 `asyncio.to_thread()`로 연결한다. 이 연결이 handler 자체의 권한 통제나 강제 종료 기능을 제공하는 것은 아니다.

### 완료와 중단

| 조건 | 대표 결과 |
|---|---|
| 유효한 단독 `task_complete(output=...)` | `completed` |
| Host 취소 | `cancelled` |
| 단계/도구 호출 한도 소진 | `limit_reached` |
| Host 응답 제한, 모델의 `length/max_tokens/content_filter` 응답 | `incomplete` |
| 빈 모델 응답 또는 실행 예외 | `failed` |

일반 텍스트에 “완료했습니다”라고 써도 완료 처리가 되지 않는다. Plan의 모든 항목을 완료로 바꾸어도 Task가 종료되지는 않는다. 실행 중 자식이나 처리할 새 입력이 있으면 완료 요청이 거부될 수 있다.

<a id="context"></a>
## 6. Context·Conversation·Memo·Plan의 차이

비슷한 이름이지만 각 데이터의 목적과 수명이 다르다.

| 개념 | 담는 내용 | 실제 예 |
|---|---|---|
| `Conversation` | 현재 Task의 원본 입력·응답·도구 호출/결과·통신 기록 | `ledger_read`에 어떤 인자를 주고 어떤 행을 받았는가 |
| `Memo` | Agent가 명시적으로 남긴 작업 메모와 출처 | “정산 완료 건만 계산한다”, 근거 참조 |
| `Summary` | 오래된 Conversation의 손실 있는 요약 | 앞선 조사 과정의 짧은 요약 |
| `Plan` | 작업 항목·의존성·상태·변경 이유·revision | 정책 확인 → 거래 조회 → 집계 |
| `Context` | **이번 LLM 호출에 실제 전달되는 메시지 묶음** | 최초 지시 + 요약 + 메모 + 계획 + 최근 대화 |
| Host `Memory` | 여러 Task에 걸쳐 유지할 장기 정보 | 검증 후 다른 작업에서도 재사용할 사실 |
| `Storage` | 자료를 저장·조회하는 수단 | TaskRecord와 StorageRecord를 DB에 보관 |

### Context를 만드는 순서

현재 `ContextBuilder.build()`는 대략 다음 순서로 메시지를 구성한다.

```text
최초 system/작업 지시/Host context
  → 과거 Summary
  → Task Memo
  → 현재 Plan
  → 동적 도구 상태(활성화한 경우)
  → 요약 범위에 들어갔지만 다시 필요한 지시·통신
  → 최근 Conversation
```

Builder는 읽기용 Snapshot에서 메시지 사본을 만든다. 생성된 Summary·Memo·Plan은 assistant 역할의 작업 자료로 유지하며 system 지시로 올리지 않는다.

압축해도 원본 Conversation은 지우지 않는다. `conversation_search`와 `conversation_read`는 요약 이전의 원문이나 LLM 입력에서 잘린 도구 결과를 다시 찾는 통로다. Host의 장기 Memory를 검색하는 도구는 아니다.

기본 압축 기준은 **48,000자**이며 정확한 토큰 예산이 아니다. 도구 출력의 LLM 표시 상한은 기본 **12,000자**, 메모 하나는 **4,000자**, Task 메모 합계는 **24,000자**다. 오래된 대화를 요약해도 큰 스키마·메모·검색 결과 때문에 Context가 계속 커질 수 있다.

### Plan은 실행기가 아니다

LLM이 `plan_create/read/update/revise`로 계획을 관리한다. `expected_revision`으로 오래된 계획에 대한 갱신을 방지하고 항목·의존성 등의 유효성을 검사한다. 그러나 계획 항목을 추가했다고 도구가 실행되거나 자식이 생성되지는 않는다. 실제 작업은 다음 LLM 판단의 도구 호출로 진행된다.

현재 메모 명칭은 `TaskMemo`, `TaskResult.memos`, `memo_write`다. 이전 `TaskMemory`, `TaskResult.memory`, `memory_write`에는 호환 별칭이 없다.

<a id="subagents"></a>
## 7. Sub-agent와 통신 구조

### 자식도 같은 Task다

```text
Root, depth=0
├─ A, depth=1
│  └─ A-1, depth=2
└─ B, depth=1

각 노드: Session + Channel + Runtime + Context + Plan + Tracer
전체 관리: Root의 SubagentManager
```

각 Task는 Context·Conversation·Memo·Plan·상태·Tracer를 따로 가진다. 부모의 모든 대화가 자식에게 자동 복사되거나 자식의 메모가 부모 메모에 자동 합쳐지지 않는다. 부모가 전달한 지시·Context와 통신·결과 회수를 통해 정보를 교환한다.

Host는 `subagents={"research": SubagentConfig(...)}`처럼 실행 profile을 제공한다. LLM은 승인된 설정 ID를 선택한다. 각 설정의 `registry/executor`는 명시해야 한다. Provider·모델·개별 한도는 생략하면 부모 설정을 사용하고, options는 생략 시 상속하지만 명시적 `None`이면 상속하지 않는다.

재위임은 `allowed_subagent_config_ids`로 허용한다. 기본 `()`는 재위임 불가다. Root에는 `max_total_subagents`와 `max_subagent_depth`를 모두 지정해야 한다. 누적 생성 수에는 이미 종료한 자식도 포함되며 완료한다고 예산이 복구되지 않는다.

### Host와 Agent의 관리 범위

| 접근 주체 | 접근 범위 |
|---|---|
| Host의 `root.subagents` | 전체 자손 목록·Session·트리 Snapshot |
| 각 Agent 내부 `SubagentScope` | 자신의 직접 자식 |
| 자손의 공개 `session.subagents` | `None`; Host는 Root Manager로 해당 Session을 찾음 |

Host가 A-1에 지시하려면 `root.subagents.child(task_id)`로 Session을 얻어 `send_instruction()`을 호출한다. Root에 새 지시를 보냈다고 모든 자식에게 자동 전파되지는 않는다.

### 질문·알림·지시의 차이

| 도구 | 뜻 |
|---|---|
| `parent_ask` / `subagent_ask` | 질문을 보내고 `request_id`를 즉시 받음 |
| `agent_reply` | 해당 요청 ID에 답함 |
| `agent_wait_reply` | 답변을 기다리되 새 입력이 오면 판단을 재개할 수 있음 |
| `parent_notify` / `subagent_send(kind="notify")` | 정보를 알림 |
| `subagent_send(kind="instruction")` | 자식의 작업 지시를 갱신 |
| `subagent_wait` / `subagent_result` | 자식 종료 대기 / 결과 조회 |

형제 A와 B의 직접 통신 API는 없다. A → 부모 → B → 부모 → A처럼 부모가 중재한다. 자식에게는 `host_ask`를 노출하지 않고 부모를 통해 필요한 결정을 받는다.

질문이나 알림은 진행 중인 LLM/도구 호출을 강제로 끊지 않는다. 새 입력을 기록하고 안전한 실행 경계에서 Context에 반영한다. 기다리던 중 새 질문이 도착해 대기가 풀려도 원래 요청은 유지된다.

<a id="tools"></a>
## 8. 도구 실행과 Host 승인

### 처음부터 제공하는 도구

Host가 `ToolSpec`에 이름·설명·JSON 스키마를 정의하고 handler와 묶어 `ToolRegistry`에 넣는다. `ToolExecutor`는 실제 handler를 호출한다.

```text
LLM의 ToolCall(name, arguments)
  → Runtime의 호출 가능 여부·한도 검사
  → SessionTools
  → ToolExecutor.execute(name, arguments)
  → Host handler(**arguments)
  → 원본 결과 기록 + LLM 표시용 결과
  → 다음 Context
```

기본 Root의 내부 도구는 9개다. `host_ask`, `memo_write`, `conversation_search`, `conversation_read`, `task_complete`와 Plan 도구 4개다. Sub-agent, 통신, 동적 도구 도구는 설정과 해당 Task의 위치·권한에 따라 추가된다.

### 실행 도중 필요한 도구를 요청하는 경우

`host_tools`는 Host가 준비한 추가 도구 카탈로그다. `allowed_tool_ids`는 그 Task에 승인할 수 있는 범위를 제한하며, 목록에 있다는 사실만으로 사용 권한이 생기지는 않는다.

```text
A-1: tool_request("ledger_read", ...)
  → A: tool_forward(request_id)
  → Root: tool_forward(request_id)
  → Host에 승인 요청 이벤트
  → Host가 원래 요청 Session에 grant_tool(...)
  → approved
  → Runtime의 다음 적용 경계에서 handler 연결
  → applied
  → 다음 LLM 요청에 스키마 제공: tool_set_offered
  → LLM이 호출하면 실제 handler 실행
```

Host는 `deny_tool()`로 거부할 수도 있다. 요청 상태에는 `requested`, `awaiting_host`, `approved`, `applied`, `denied`, `apply_failed`, `closed`가 있다.

**승인, 적용, 모델에 제공, 실제 실행은 서로 다른 사건**이다. 자연어로 “써도 된다”고 답하거나 부모가 도구를 갖고 있다는 이유만으로 자식에게 권한이 생기지 않는다. 동적 승인은 원래 요청한 Task의 전용 추가 Registry에 적용한다.

현재 Tool Integration은 이미 Host가 구현한 도구를 연결한다. 플러그인 설치나 임의 코드 다운로드 기능은 없다. 동적 도구 기능을 활성화한 상태에서 기본 Registry를 외부에서 바꾸면 변경을 감지해 거부한다.

<a id="observation"></a>
## 9. 상태·이벤트·Tracer·결과 읽기

### 서로 다른 네 가지 질문

| 알고 싶은 것 | 사용하는 데이터/API |
|---|---|
| 이 작업은 끝났나, 부모는 누구인가? | `TaskState` |
| 지금 모델 호출 중인가, 답변을 기다리는가? | `AgentState` |
| 어떤 순서로 일이 일어났나? | `AgentEvent`, `session.events()` |
| 그때 정확히 어떤 인자와 응답이 오갔나? | `TraceArtifact`, `session.artifact(id)` |

Task 상태가 `running`이어도 Agent 활동은 `llm_call`, `tool_execution`, `waiting_for_subagents` 등으로 달라질 수 있다. `TaskChannel`이 두 상태를 소유하고 함께 조회할 수 있게 한다.

아래 코드는 이미 시작된 `session`을 사용하는 조회 예시다.

```python
state = await session.state()
print(state.task.status, state.agent.activity)

# 상태와 같은 시점의 cursor 이후부터 이어서 관찰한다.
async for event in session.events(
    after_sequence=state.last_event_sequence
):
    print(event.sequence, event.type)
    for artifact_id in event.artifact_ids:
        detail = await session.artifact(artifact_id)
        print(detail.kind)
```

이벤트의 `sequence`는 **Task별 순서**다. 여러 Task의 sequence를 한 전역 순서로 비교하면 안 된다. Task 사이 관계는 Task ID·요청 ID·호출 ID·Artifact 참조로 따라간다.

Host 명령 API의 반환은 접수 확인이다. `answer()`나 `send_instruction()` 호출이 끝났다는 사실만으로 적용을 단정하지 말고 `command_applied` / `command_rejected`를 확인한다.

### 최종 결과

`TaskResult`에는 다음 정보가 모인다.

- 실행 결과: `task_id`, `status`, `output`, `stop_reason`
- 실행 수치: `steps`, `tool_calls`, `tool_errors`, `warnings`
- 작업 정보: `messages`, `memos`, `context_summary`, `context_compactions`, `plan`
- 기록: 현재 Task의 `tracer`, Sub-agent 기능을 활성화한 Root의 `tree`

```python
from shs_agent import TaskTreeSnapshot

result = await root.result()
if result.tree is not None:
    saved = result.tree.to_json(indent=2)
    restored = TaskTreeSnapshot.from_json(saved)
    print(restored.result(root.task_id)["output"])
else:
    saved = result.tracer.to_json(indent=2)
```

단일 Task는 `tracer`로 읽는다. 전체 Tree는 Root 결과에 한 번 포함되며, 각 Task의 독립적인 trace를 보존한다. Tree JSON은 현재 schema 2이고 이전 직접 자식 Tree schema 1을 읽을 수 있다.

실시간 구독을 하지 않아도 기록은 남는다. JSON을 읽는 Replay는 **실행 이력의 재조회**다. 중단된 실행의 Resume나 LLM/도구 재실행이 아니다.

### “성공”으로 혼동하기 쉬운 값

| 값 | 정확한 의미 |
|---|---|
| `result.status == completed` | 엔진이 정상 완료 절차를 수용함 |
| `tracer.complete` / `tree.complete` | 해당 실행 기록의 종료 여부 |
| `details_complete` | 선언된 수집 범위 안에서 상세 수집이 완전한가 |
| 예제의 `business_review` | Host가 실제 출력과 근거를 검토한 업무 수용 결과 |

기록에는 공통 LLM 요청/응답과 adapter가 관측한 SDK 자료, 공개된 thinking 필드, 도구 인자·결과 등이 들어간다. HTTP 원문이나 모델의 비공개 추론을 복원하는 기능은 아니다.

기록은 현재 인메모리이며 자동 삭제하지 않는다. 긴 작업은 상세 이력과 Snapshot 복사 비용이 증가한다. 이 부분은 향후 보관 정책과 함께 검토할 대상이다.

<a id="reading"></a>
## 10. 실행해 보면서 코드 읽기

### 가장 먼저 볼 실행 예제

저장소 루트의 준비된 Python은 `.venv\Scripts\python.exe`이며 확인된 버전은 3.12.9다. 기본 `python`은 3.10이므로 이 프로젝트의 요구 버전인 3.11 이상과 다르다.

```powershell
# 외부 모델 없이 전체 흐름 확인
.\.venv\Scripts\python.exe examples/full_workflow_host.py `
  --review-memory --output-dir build/guide_demo

# 같은 실행의 저장 기록 조회
.\.venv\Scripts\python.exe examples/replay_task_tree.py `
  build/guide_demo/task_tree.json

# 오프라인 회귀 테스트
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

첫 예제는 `task_tree.json`, `live.jsonl`, `summary.json`을 저장한다. 같은 출력 디렉토리를 다시 사용하면 해당 파일들을 갱신한다. Fake Provider는 미리 정해 둔 판단 흐름으로 **실제 Runtime의 ToolCall 경로**를 실행하며, 조회 handler 결과를 사용한다. 실제 LLM의 추론 능력을 측정하는 예제는 아니다.

Windows sandbox에서는 기존 보고서와 이번 확인 모두 비동기 테스트가 진행되지 않는 현상이 있었다. 코드의 실패 판정과 실행 환경 제약을 구분해야 한다.

### 전체 예제의 이야기

1. Root가 Plan을 만들고 A와 B를 시작한다.
2. A는 A-1을 시작한다. A-1의 집계 기준 질문은 부모와 Root를 거쳐 B에게 전달된다.
3. B는 `policy_read` handler로 확인한 기준을 답한다.
4. A-1은 고객 조회 도구를 요청하지만 Host가 거부한다.
5. A-1은 계획을 수정하고 거래 원장 도구를 요청한다. Host 승인 뒤 실제 도구가 적용된다.
6. 원장 handler에서 읽은 자료로 3건·2,500원을 계산하고 메모·계획·결과를 정리한다.
7. 부모들이 결과를 회수한다. Host는 별도로 답을 검증하고 Live 기록과 최종 Tree를 비교한다.
8. `--review-memory`가 있으면 출처 있는 장기 Memory **후보**를 추출한다. 자동 저장하지 않는다.

[full_workflow_host.py](../../examples/full_workflow_host.py)는 실제 Host 사용 패턴이고, [workflow_support.py](../../examples/workflow_support.py)는 합성 데이터·handler·Fake 판단과 제어 Gate를 제공한다. `TreeObserver`는 예제의 Host helper다. 이를 별도 엔진이나 필수 공용 메시지 버스로 이해할 필요는 없다.

관찰 coroutine은 사용자 답변을 기다리며 멈추지 않게 구성되어 있다. Host 결정 처리는 별도 Queue와 coroutine이 담당한다. 실제 UI에 연결할 때도 이 분리가 중요하다.

### 권장 코드 읽기 순서

| 순서 | 읽을 코드 | 읽고 나서 답할 수 있어야 할 질문 |
|---|---|---|
| 1 | [types.py](../../src/shs_agent/agent/types.py), [state.py](../../src/shs_agent/agent/state.py) | 입력, 결과, 상태, 실행 한도가 무엇인가? |
| 2 | [solver.py](../../src/shs_agent/agent/solver.py)의 `_assemble_task()` | 어떤 객체가 Task마다 생성되는가? |
| 3 | [handle.py](../../src/shs_agent/agent/session/handle.py) | Host가 호출할 수 있는 API는 무엇인가? |
| 4 | [runner.py](../../src/shs_agent/agent/runtime/runner.py)의 `_loop/_run_batch/_finish` | 판단·도구·지시·종료가 어디서 연결되는가? |
| 5 | [core.py](../../src/shs_agent/agent/core.py), [builder.py](../../src/shs_agent/agent/context/builder.py) | 모델에 무엇을 주고 어떤 응답을 받는가? |
| 6 | [session_tools.py](../../src/shs_agent/agent/tools/session_tools.py), [executor.py](../../src/shs_agent/tool/executor.py) | 모델의 도구 호출이 실제 함수까지 어떻게 도달하는가? |
| 7 | [manager.py](../../src/shs_agent/agent/subagent/manager.py), [scope.py](../../src/shs_agent/agent/subagent/scope.py), [integration.py](../../src/shs_agent/agent/tools/integration.py) | 위임 범위와 도구 권한을 누가 통제하는가? |
| 8 | [test_full_workflow.py](../../tests/test_full_workflow.py), [full_workflow_host.py](../../examples/full_workflow_host.py) | 성공·실패·제어·관찰 계약을 어떤 증거로 확인하는가? |

### 기능을 바꿀 때 찾아갈 위치

| 변경하려는 내용 | 먼저 볼 곳 |
|---|---|
| 작업 생성 옵션과 기본값 | `agent/types.py`, `agent/solver.py` |
| 반복 횟수, 취소, 완료, 응답 처리 | `agent/runtime/runner.py` |
| Context 구성과 압축 | `agent/context/builder.py`, `compaction.py`, Runtime의 `_compact()` |
| 계획 규칙 | `agent/planning/plan.py` |
| 내부 도구의 인자·설명·처리 | `agent/tools/schemas.py`, `session_tools.py` |
| 새 외부 업무 도구 | Host의 handler와 `tool/` 등록 코드 |
| 자식 한도와 위임 범위 | `agent/subagent/manager.py`, `scope.py`, `models.py` |
| 부모·자식 메시지 처리 | `communication/`, `subagent/manager.py`, `session/channel.py` |
| 추가 도구 승인·적용 | `agent/tools/integration.py`, `session_tools.py` |
| 모델 API 지원 | `llm/provider/`, `llm/types.py`, `llm/observation.py` |
| 관찰 화면과 기록 출력 | 먼저 `TaskSession` 공개 API와 Host 예제; 기록 구조 변경 시 `tracer/` |
| DB 저장 기능 연결 | `storage/`와 Host 보관 경계; 현재 자동 연결은 없음 |

<a id="next"></a>
## 11. 남은 문제와 다음 작업 순서

아래 순서는 코드와 기존 실험 결과를 바탕으로 한 **개발 제안**이다. 이미 구현한 기능이나 확정된 일정으로 읽지 않는다.

### 먼저 현재 개발 기준을 고정

**문서 이동에 맞춰 참조와 배포 설정을 정리하고, 현재 실행 가능한 상태를 커밋으로 남기는 작업이 먼저다.**

이번 조사에서 실제 문서는 `docs/dev/`에 있는데 다음 파일에는 이전 경로가 남아 있음을 확인했다.

| 위치 | 남아 있는 경로 예 | 현재 위치 |
|---|---|---|
| `README.md` | `docs/HOST_USAGE.md`, `docs/reports/...` | `docs/dev/HOST_USAGE.md`, `docs/dev/reports/...` |
| `pyproject.toml`의 data-files | `docs/HOST_USAGE.md`, `docs/architecture/...`, `docs/reports/...` | 각 `docs/dev/...` |
| `tests/verify_distribution.py` | wheel 데이터 경로를 checkout의 같은 상대 경로와 비교 | 소스 경로와 배포 경로를 어떻게 유지할지 함께 정리 필요 |

기존 배포 통과 기록은 당시 상태의 증거다. 현재는 경로 참조가 어긋나므로 새 빌드와 배포 검증을 다시 수행해야 한다. 이번 문서 작성에서는 이 코드와 설정을 수정하지 않았다.

### 다음은 실제 모델의 실패를 작은 단위로 줄이기

| 우선 작업 | 이유 | 완료를 판단할 증거 |
|---|---|---|
| Context 예산·검색 결과 중첩 점검 | 문자 수 기반 압축만으로 긴 입력 문제를 모두 제어하지 못함 | 고정 실패 사례에서 입력 크기·출력 제한·원문 접근을 함께 확인 |
| 도구 인자 오류 안내 개선 | 실제 모델이 지원하지 않는 인자를 사용함 | 잘못된 인자와 유효 스키마를 이해 가능한 오류로 제공하고 재시도 경로 확인 |
| 업무 데이터 스키마와 정답 기준 명시 | 엔진 completed만으로 답의 정확성을 보장하지 못함 | 실제 조회 근거와 결과 필드/값을 Host가 검증 |
| Host의 복구 질문 처리 | 기존 고정 응답 Host가 복구 질문에도 같은 정책만 반복함 | 질문 종류별 처리, 복구 거절/허용, 반복 상황 종료를 확인 |
| 빈 응답의 원인 관찰 | SDK 응답부터 비어 있던 사례가 있음 | 모델·옵션·서버 조건별 증거를 수집하고 실패를 보존 |

재시도나 모델 전환은 Host가 허용하는 정책으로 정해야 한다. 현재 자동 재시도·자동 모델 전환 정책이 완성되어 있다는 전제로 새 기능을 붙이면 안 된다.

### 그다음 실제 Host에 연결

현재 API만으로 CLI나 UI에서 작업 시작, 실시간 상태 표시, 질문 응답, 도구 승인, 특정 자식 제어, 결과 검토를 연결할 수 있다. 우선 실제 업무 하나를 좁게 정하고 `full_workflow_host.py`의 Fake Provider·고정 응답을 실제 Provider와 사용자 입력으로 바꾸는 접근이 적절하다.

새 GUI, 장기 Memory, Storage 자동 저장, Checkpoint/Resume, Workspace/Git 격리, Token Streaming, 분산 실행은 현재 통합 범위에 포함되지 않는다. 특히 저장된 JSON이 있다는 이유만으로 Resume가 가능한 상태는 아니다. 이런 확장은 기존 엔진 계약 위에서 별도 설계해야 한다.

<a id="evidence"></a>
## 12. 검증 근거와 참고 문서

### 이번 문서 작성 시 확인한 범위

- 2026-10-20 KST 현재 작업 폴더의 소스, 공개 API, 주요 실행 분기와 Git 상태를 대조했다.
- 8단계 보고서와 실제 Ollama 보고서, 7단계 실제 모델 비교 보고서를 분리해서 읽었다.
- 현재 테스트 메서드가 240개임을 정적으로 확인했다.
- 현재 오프라인 테스트 재실행 결과는 아래 검증 기록에 적었다.
- 이번에 실제 Ollama/OpenAI 호출, 모델 다운로드, 배포 빌드 검증은 수행하지 않았다. 실제 모델·배포 결과는 날짜가 명시된 기존 보고서의 기록이다.

**현재 오프라인 테스트 재확인:** 2026-10-20 KST, 저장소의 Python 3.12.9에서 `unittest discover -s tests -v`를 실행하여 **240개 모두 통과, 47.884초**를 확인했다. Windows sandbox 안에서는 첫 비동기 테스트가 진행되지 않아 50초 제한으로 중단했고, sandbox 밖에서 같은 테스트를 실행해 통과했다. 성공 로그는 `build/docs_overview/unittest-20261020.log`에 있다. 이 로그는 로컬 검증 산출물이며 버전 관리 대상이 아니다.

### 테스트를 찾는 지도

| 관심 영역 | 테스트 |
|---|---|
| 기본 계약·실행·제어 | `test_contracts.py`, `test_runtime.py`, `test_session.py` |
| Context·원문·입력 조합 | `test_context.py`, `test_context_builder.py` |
| 상태·상세·SDK 관측 | `test_state_details.py`, `test_tracer.py`, `test_adapter_observation.py` |
| 계획 | `test_planning.py` |
| 위임·재귀·통신 | `test_subagent.py`, `test_recursive_subagent.py`, `test_communication.py` |
| 도구 승인 | `test_tool_integration.py` |
| 전체 Host 사용 흐름 | `test_full_workflow.py` |
| 배포물 독립 실행 | `verify_distribution.py` |
| 별도 실제 모델 실험 | `verify_ollama_*.py`, `examples/ollama_workflow_host.py` |

### 원본 문서

- [Host 사용 계약](HOST_USAGE.md): 현재 API, 입력 처리와 실행 예제.
- [최종 목표 아키텍처](architecture/SHS_AGENT_TARGET_ARCHITECTURE.md): 합의된 방향과 책임 경계. 현재 구현 완료 목록과는 구분한다.
- [1단계 보고서](reports/phase_1/report.md), [Tracer 보고서](reports/phase_2_tracer/phase_2_tracer.md), [State·상세 Tracer 보완](reports/phase_2_state_tracer/phase_2_state_tracer.md).
- [3단계 Planning](reports/phase_3_planning/phase_3_planning.md), [4단계 Sub-agent](reports/phase_4_subagents/phase_4_subagents.md), [5단계 Communication](reports/phase_5_communication/phase_5_communication.md).
- [6단계 재귀 위임](reports/phase_6_recursive_subagents/phase_6_recursive_subagents.md), [7단계 Tool Integration](reports/phase_7_tool_integration/phase_7_tool_integration.md).
- [7단계 실제 모델 비교·문제 분석](reports/phase_7_tool_integration/ollama_comparison_architecture_report.md).
- [8단계 전체 통합](reports/phase_8_integration/phase_8_integration.md), [8단계 실제 모델 소규모 검증](reports/phase_8_integration/ollama_report.md).

이 문서는 현재 구조를 읽기 위한 안내서다. 코드가 바뀌면 공개 계약과 실행 분기를 먼저 확인하고, 구현 완료·실험 성공·향후 제안을 구분해서 갱신한다.
