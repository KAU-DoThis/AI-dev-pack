# 8단계 실제 Ollama 소규모 통합 검증

실행: **2026-10-18 19:33:55 KST**, 로컬 `gemma4:e4b`, 한 번의 Root workflow.
결과: **엔진 completed/task_complete, Host 업무 수용 true**. 작은 합성 거래에서 정산 완료
3건, 총 2,500원, ID T1/T2/T4, 통화 KRW를 반환했다.

## 실행 조건과 증거

- 실행 스크립트: `examples/ollama_workflow_host.py --model gemma4:e4b --quiet`
- 이미 설치된 모델 digest: `c6eb396dbd5992bbe3f5cdb947e8bbc0ee413d7c17e2beaae69f5d569cf982eb`
- Provider: 기존 `OllamaLLM`, 서버 `http://localhost:11434`, Python 3.12.9.
- Root와 자식에 같은 모델, temperature=0, seed=73, think=false, num_ctx=8192, num_predict=1024.
- Task별 max_steps=12, max_tool_calls=24, Root 전체 자손 수 1, 깊이 1. 압축 비활성화.
- 실행 중 사용자 결정은 별도 Host coroutine이 실제 question_id에 통화 KRW를 답했다.
- 데이터는 Host가 등록한 `ledger_read`, `policy_read` handler를 통해서만 제공했다.
- 경과 약 **20.20초**. 모델 로드/추론과 Task 실행·결과 회수를 포함하는 측정이며 성능 벤치마크가 아니다.

저장 자료는 저장소의 `build/phase8_ollama_20261018T103355Z/`에 있다.
`task_tree.json`에 전체 LLM/도구/상태/결과, `live.jsonl`에 Host 관찰,
`summary.json`에 엔진 종료·업무 수용·기능별 관찰 여부를 기록했다.
터미널 로그는 `build/phase8-ollama.log`다. build 폴더는 기존 gitignore 정책대로 버전 관리 대상이 아니다.

## 실제 결과

| Task | 종료 | LLM 호출 | 도구 호출 | 도구 오류 | 이벤트 / Artifact |
|---|---|---:|---:|---:|---:|
| Root | completed/task_complete | 9 | 9 | 2 | 198 / 89 |
| 자식 | completed/task_complete | 4 | 4 | 0 | 84 / 40 |
| 합계 | 2개 Task 종료 | 13 | 13 | 2 | 282 / 129 |

실제 Host handler는 `ledger_read`, `policy_read` 각각 한 번 실행했다. 최종 JSON은 다음과 같다.

```json
{"count": 3, "total": 2500, "ids": ["T1", "T2", "T4"], "currency": "KRW"}
```

Host는 출력의 실제 필드·값, 성공한 ledger handler 실행 및 엔진 완료를 평가했다.
Tree의 complete/details_complete는 true이고 Task별 모든 Live 이벤트·cursor 상태·수집 참조가 최종 Tree와
일치했다. 저장 JSON으로도 조회할 수 있으며 실제 모델 재호출이 필요하지 않다.

## observed와 not_observed

| 기능 | 이번 실제 실행 |
|---|---|
| LLM 호출, Plan 생성 | observed |
| Host 질문의 답변 적용 | observed |
| 직접 자식 생성·대기·결과 전달 | observed |
| 실제 ledger/policy 조회 및 정답 산출 | observed |
| 전체 Tree, Live 일치, 상세 기록 종료 | observed |
| Replan | not_observed |
| 깊이 2 이상 재귀 | not_observed: 이번 설정은 깊이 1 |
| Agent 간 질문·답변 | not_observed |
| 동적 도구 요청의 거부·승인·적용 | not_observed: 정적 도구를 자식에게 제공 |
| 중간 지시·명시적 취소·실패 정리 | not_observed |

Fake 전체 통합에서 성공한 기능을 이 표의 실제 모델 관찰로 옮기지 않았다. 선행 사건 없이 `all([])`로
성공 처리하지 않고, `observed_features()`는 실제 사건 존재를 확인한다.

## 오류와 해석

Root가 `subagent_spawn`에 지원하지 않는 `task_id` 인자를 두 번 넣었다. 실제 도구 호출 결과는
`TypeError: SessionTools._subagent_spawn() got an unexpected keyword argument 'task_id'`로 기록됐다.
Root의 task-local sequence 71, 91의 `tool_finished`에서 확인할 수 있다. 모델이 다음 판단에서 인자를
고쳐 세 번째 생성 요청으로 자식을 만들었다. Host가 강제로 workflow를 재실행하거나 오류 응답을 교체한
것이 아니다. 오류 횟수를 지우지 않았으며 엔진을 모델에 맞춰 수정하지 않았다.

분류: 관찰된 두 오류는 **모델의 도구 인자 사용 오류**다. 이번 실행에서 Host 환경 실패나 엔진 실패는
관찰되지 않았다. 이는 모든 엔진 결함이 없다는 증거가 아니다. 특히 이전 7단계에서 드러난 빈 응답·큰
Context·복구 대응 문제를 해결했음을 뜻하지 않는다.

작은 고정 자료, 1개 모델, 1회 실행이다. 이전 복잡한 결제 사고의 0/4 업무 실패와 난이도·구성·데이터가 다르다.
일반 성공률, 모델 순위, 장시간 안정성, GPU 병렬 성능으로 일반화할 수 없다. 다른 모델·실서비스·SQLite·
동적 권한·전체 재귀/통신 프로토콜의 실제 모델 품질은 이번 실험 범위 밖이다.

모델 다운로드, 서버 설정 변경, Host 자동 재시도, 결과 수정은 하지 않았다. 요청 옵션 keep_alive=1m은
이 호출의 보유 시간이며 서버 영구 설정을 변경하지 않았다. 실제 실행 후 기능 범위를 추가 관찰한 것처럼
표시하지 않았다.
