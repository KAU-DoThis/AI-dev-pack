**5단계 Agent Communication — 실제 Ollama 검증 보고서**

실행일: 2026-10-18, Asia/Seoul. Gemma와 Qwen을 순차적으로 각각 **1회** 실행했다. Gemma는 **A → Root → B → Root → A의 실제 통신 왕복에 성공**했지만 최종 산출물 오류가 있어 전체 시나리오 통과로 판단하지 않았다. Qwen은 B의 request_id 선택 오류와 Root의 빈 SDK 응답으로 왕복을 완료하지 못했다. 두 실행 모두 B의 도구 실행 중 질문 접수와 도구 종료 후 Context 적용은 확인했다.

엔진 구현은 수정하지 않았다. 실행 전후 `src`의 Python 파일 64개 SHA-256이 동일하며, 실제 실행 후 기존 전체 unittest **167개가 13.489초에 통과**했다. 재실행, 모델 다운로드·대체, 서버 재설정·재시작, 라이브러리 설치, commit/push는 하지 않았다.

**검증 환경과 방법**

먼저 다음 기존 자료와 실제 Provider/도구 스키마를 확인했다.

- [5단계 구현 보고서](phase_5_communication.md)
- [기존 통신 예제](../../../examples/communication.py): 통신 구조 참고용이며 이번 모델 응답 생성에는 사용하지 않았다.
- [통신 도구 스키마](communication_tools.schema.json), [Root 내부 도구](root_internal_tools.schema.json), [자식 내부 도구](child_internal_tools.schema.json)
- [기존 Ollama Sub-agent 검증](../../../tests/verify_ollama_subagents.py), 기존 `OllamaLLM`과 SDK 어댑터

| 설정 | 실제 사용 값 |
| --- | --- |
| 서버 | 기존 `http://localhost:11434`, `/api/version` 및 `/api/tags` 확인 |
| Ollama / SDK / Python | 0.32.15 / 0.6.2 / 3.12.9 |
| Gemma | `gemma4:e4b`, 8.0B, Q4_K_M, 설치 확인 |
| Qwen | `qwen3.5:9b`, 9.7B, Q4_K_M, 설치 확인 |
| 생성 설정 | temperature=0, max_tokens=4096, think=true, num_ctx=16384 |
| Root 한도 | max_steps=28, max_tool_calls=60, max_total_subagents=2, max_subagent_depth=1 |
| 자식 한도 | max_steps=18, max_tool_calls=32 |
| 테스트 시간 제한 | SDK HTTP 180초, gate 180초, 모델별 watchdog 600초 |
| Context 압축 / Host 응답 | 압축 비활성화, Host 질문·답변 대행 없음 |

모델 digest는 [metadata.json](../../../build/ollama_communication/20261017T153505Z-bee3c79e/metadata.json)에 보존했다. Gemma `c6eb396dbd5992bbe3f5cdb947e8bbc0ee413d7c17e2beaae69f5d569cf982eb`, Qwen `6488c96fa5faab64bb65cbd30d4289e20e6130ef535a93ef9a49f42eda893ea7`이다. 로컬 연결과 asyncio 소켓을 위해 실행 권한만 사용했으며 서버 설정은 변경하지 않았다.

[검증 스크립트](../../../tests/verify_ollama_communication.py)는 기존 `ProblemSolver`, `SubagentConfig`, `OllamaLLM`, `ollama.Client`를 사용한다. 각 모델마다 새로운 Root와 별도의 무작위 fixture를 만들었다. 한 Root와 그 A/B는 같은 모델을 사용했다. Root가 실제 `subagent_spawn`으로 B와 A를 생성했고, Host는 프로필·프롬프트·한도와 테스트 도구를 준비하고 이벤트만 관찰했다. 모든 도구명과 인자는 실제 SDK 응답에서 생성됐으며, 전체 normalized tool call의 이름·인자가 SDK 응답과 일치함을 오프라인으로 확인했다. 프로토콜을 명시한 프롬프트에 대한 1회 관찰이며 모델의 일반적인 성공률을 측정한 실험은 아니다.

실제 도구 스키마에서 `parent_ask(message, related_request_id?)`와 `subagent_ask(task_id, message, related_request_id?)`는 새 request_id를 반환한다. `agent_reply(request_id, message)`는 기록된 요청의 상대에게 답하며 수신자를 임의 지정하지 않는다. `agent_wait_reply(request_id)`는 대기 해제와 요청 취소를 구분한다. `subagent_send`와 `parent_notify`도 기존 스키마에서 확인했지만 이번 흐름에서는 호출되지 않았다.

B에만 `read_backend_contract`를 등록했다. 인자는 빈 객체이고 반환값은 실행별 fixture이다. Root의 18개 내부 도구, A의 12개 내부 도구에는 이 외부 도구가 없으며 B는 내부 12개와 외부 1개를 받았다. Root/A 최초 실제 LLM 요청에 무작위 정답 필드·값·source_ref·revision이 없는 것도 확인했다.

도구 진입 후 테스트 전용 `asyncio.Event`를 기다리고, B의 실제 `agent_message_received(kind=question)` 관찰 시 gate를 열었다. 질문이나 답변을 주입하지 않았고 LLM 선택을 대체하지 않았다. 두 실행 모두 watchdog·gate timeout은 발생하지 않았다. Qwen의 종료 취소는 watchdog이 아니라 기존 부모 종료 처리에서 발생했다.

**모델별 비교**

| 항목 | Gemma `gemma4:e4b` | Qwen `qwen3.5:9b` |
| --- | --- | --- |
| Root / A / B 최종 상태 | completed / completed / completed | failed / cancelled / cancelled |
| 종료 이유 | 모두 task_complete | Root empty_response, 자식 host_cancelled |
| 전체 LLM 호출 | 18: Root 10, A 4, B 4 | 9: Root 4, A 2, B 3 |
| 모델이 생성한 도구 호출 | 21: Root 11, A 6, B 4 | 9: Root 4, A 2, B 3 |
| 실제 실행 진입 도구 호출 | 19: Root 11, A 4, B 4 | 8: Root 4, A 2, B 2 |
| 실제 질문 / 수락된 답변 | 2 / 2 | 2 / 0; B의 답변 실행 시도 1회 실패 |
| A→Root→B→Root→A | **통신 왕복 성공**, 최종 작업 품질은 실패 | **실패**, B→Root에서 연결되지 못함 |
| request_id / 관련 요청 연결 | qA≠qB, qB.related_request_id=qA | qA≠qB, 관련 연결 정상. B가 qA로 답변 시도 |
| 원래 작업 재개 및 완료 | A 산출물·B 표/체크리스트·Root 회수 확인. A 산출물 오류 있음 | 읽기까지 완료, 답변 후 원래 작업 재개·완료 미달성 |
| 도구 오류 | A의 잘못된 batch 1개: 도구 2개 skipped | B agent_reply 1개 failed |
| 미응답 질문 / 종료 후 활성 요청 | 0 / 0 | **2 / 0**; 종료 시 닫힘은 답변 성공이 아님 |
| B 실행 중 질문 / 안전 경계 적용 | 확인 | 확인 |
| 부모 대기 진입 시 이미 온 질문 처리 | 확인, qA 보존 | 확인, qA 보존 |
| 부모가 대기 중일 때 새 질문 도착 | **미관찰** | **미관찰** |
| agent_wait_reply 중 새 질문 처리 후 기존 요청 재대기 | **미관찰** | **미관찰** |
| Tracer 완전성 | 3개 완전, 436 events / 216 artifacts | 3개 완전, 239 events / 113 artifacts |
| 실행 관찰 시간 | 76.682초 | 38.269초 |
| 전체 엄격 검증 | 실패 | 실패 |

`tool_calls` 상태 카운터는 실제 실행 진입 수이다. Gemma의 실행 전 거절된 2개 호출과 Qwen의 취소 후 폐기된 마지막 LLM 응답 내 1개 호출은 이 수에 포함되지 않는다. 모델 생성 수·실행 수·오류 수를 구분했다. Gemma의 같은 Task 안에서 오류를 받은 뒤 이어진 LLM 호출은 정상 실행 루프이며, Host가 전체 테스트를 재시도한 것이 아니다.

**실제 식별자와 테스트 명세**

| 구분 | Gemma | Qwen |
| --- | --- | --- |
| Root task_id | `0a8e9ea71f4a46e4b1829c68b93786cc` | `671ca3bb99474e37adc29124604fcd47` |
| A task_id | `f1432a7158664fe8a4689ff14dd16f40` | `70f77b9f38b944989ed3ee23288ca6d0` |
| B task_id | `e4d81d7bf631487cb47b4cb11899eafb` | `d29d3f05a6104e6fada0c1a39e96e174` |
| qA | `06d56c812a3243a4b381fb7353c8a3eb` | `aae019696a714f918a360d148a8b6c39` |
| qB | `71b6ec088c0140738f887eb416cbf4c6` | `3fb5f4dae9124d59a3ee9d5004bbd527` |
| 무작위 필드 | `ui_hint_edf2c2e106` | `ui_hint_3c21cd11bb` |
| 무작위 값 | `synthetic_1dff651adcc0450bba` | `synthetic_0644694a15284da5bb` |
| schema_revision | `rev_b045adbf6cc3` | `rev_d145d098bd2e` |

Gemma에서 B의 도구가 반환한 원본 명세는 다음과 같다. Qwen 명세는 같은 구조에서 위 표의 무작위 필드·값·revision과 `source_ref=fixture:backend-contract:3c21cd11bbd145d098bd2eb89897bc68`을 사용한다. `access_token`은 문자열 모양의 **무효 테스트 값**이며 실제 인증 정보가 아니다.

```json
{
  "source_ref": "fixture:backend-contract:edf2c2e106b045adbf6cc3eff3db00f0",
  "schema_revision": "rev_b045adbf6cc3",
  "endpoint": "/api/login",
  "method": "POST",
  "success_status": 200,
  "response_example": {
    "access_token": "SYNTHETIC_ONLY_NOT_VALID",
    "user": {"id": 17, "display_name": "fixture-user"},
    "ui_hint_edf2c2e106": "synthetic_1dff651adcc0450bba"
  },
  "field_types": {"access_token": "string", "user": "object", "ui_hint_edf2c2e106": "string"},
  "frontend_required_fields": ["access_token", "user", "ui_hint_edf2c2e106"]
}
```

**Gemma 실제 메시지와 처리 순서**

아래 시각은 모두 2026-10-18 KST이다. `Root #59` 같은 표기는 해당 Task 안의 event sequence이며 전역 번호가 아니다.

| 시각 | 실제 동작 / 입력·출력 | Tracer 근거 |
| --- | --- | --- |
| 00:35:16.459–16.470 | Root가 B, A를 config_id=backend/frontend, context=[]로 생성 | Root #22/#29 subagent_created, #24/#31 tool_finished |
| 00:35:18.155 | B가 `read_backend_contract({})` 진입, gate 닫힘 | B #21 tool_started, fixture_operations |
| 00:35:19.877 | A→Root `parent_ask`: “I need the complete login contract, including its runtime-specific fields and source_ref, for frontend handling.” → qA 반환 | A #22 요청, Root #41 접수 |
| 00:35:21.678–21.682 | Root가 subagent_wait 진입 후 incoming_request로 즉시 해제. qA를 Context에 적용 | Root #52 → #55 → #59 applied → #66 context_built |
| 00:35:23.214 | A는 qA의 agent_wait_reply 진입 | A #52 |
| 00:35:27.766 | Root→B `subagent_ask`: “Please provide the complete login contract, including its runtime-specific fields and source_ref, as requested by the frontend agent.”; related_request_id=qA → qB 반환 | Root #78 요청, B #23 접수 |
| 00:35:27.767–27.774 | gate 해제, 명세 도구 정상 반환, 그 다음 qB를 B Context에 적용 | B #26 tool_finished → #28 applied → #35 context_built |
| 00:35:33.980 | B→Root `agent_reply(qB, message)`: 위 명세 전체를 `{"ok":true,"output":명세}` 형태로 전달 | B #47 요청/#51 도구 완료, Root #93 접수 |
| 00:35:35.857–35.861 | Root agent_wait_reply(qB)는 이미 온 답변으로 answered 반환. B 답변을 다음 Context에 적용 | Root #105 → #107 → #111 applied → #118 context_built |
| 00:35:39.540 | B가 답변 후 원래 조사 작업을 계속하고 메모 저장 | B #72 memo_write 완료 |
| 00:35:44.797 | Root→A `agent_reply(qA, message)`: B가 보낸 명세와 포장을 그대로 전달. A의 실제 대기 해제 | Root #130/#134, A #54 접수 → #57 answered → #61 applied → #68 context_built |
| 00:35:52.637 | B가 필드/타입 표와 검증 체크리스트로 완료 | B #91/#93 |
| 00:36:03.072 | A가 memo_write와 task_complete를 같은 응답에 생성해 모두 skipped | A #78/#80, invalid_tool_batch |
| 00:36:10.350 | A가 다음 결정에서 task_complete 단독 호출로 완료 | A #99/#101 |
| 00:36:10.357–12.475 | Root 자식 대기 종료, 두 subagent_result 회수 | Root #158/#160/#180/#200 |
| 00:36:16.125–22.760 | Root 자체 메모, 최종 검토 결과 작성 및 완료 | Root #220/#239/#242 |

B→Root와 Root→A의 `message` 문자열은 완전한 명세를 담는다. 전달 경로의 두 답변은 같은 테스트 필드·값·source_ref·revision을 포함하며, 수신자의 실제 공통 요청 및 Ollama SDK 요청에서도 확인된다. 전체 문자열과 각 도구 인자는 [Gemma trace](../../../build/ollama_communication/20261017T153505Z-bee3c79e/gemma4_e4b.trace.json)에 그대로 보존했다.

세 Task의 완료와 별도로 산출물에는 다음 문제가 있다.

- **A의 최종 output은 JSON으로 파싱되지 않는다.** 마지막 객체 닫는 `}`가 없고, `json.loads`는 `Expecting ',' delimiter: line 31 column 1133 (char 1903)`로 실패한다. `task_complete.output` 스키마는 문자열이므로 엔진의 완료 판정과 요청한 JSON 형식의 충족은 다른 사항이다.
- Root와 B의 최종 `contract`는 fixture 객체 대신 `{"ok":true,"output":fixture}`이다. 내부 `contract.output`은 원본과 정확히 같지만, 스크립트의 엄격한 `contract == fixture` 검사는 실패한다. B 답변의 포장까지 “정확한 도구 결과”로 해석할 여지도 있으므로, 이 형식 차이 자체를 통신 데이터 손실로 해석하지 않았다.
- A의 원문 의사코드는 무작위 필드 이름을 사용하지만 `response.output.success_status`를 확인한 뒤 `output.access_token`, `output.user`, `output.ui_hint_edf2c2e106`을 읽는다. 명세 객체에서는 이 필드들이 `response_example` 안에 있고, 실제 응답 body는 명세 객체와 구조가 다르다. 도구 포장·명세·실제 응답을 혼동한 코드이며 정확한 로그인 처리 구현으로 인정하기 어렵다. 의사코드를 별도 앱으로 실행한 검증은 하지 않았다.
- Root의 최종 검토는 A가 올바르게 처리했다고 평가했지만 위 오류를 발견하지 못했다. 원문 평가를 수정하지 않았다.
- 테스트 보조 조건인 개인 메모 marker는 A만 저장했다. Root/B도 각자 메모를 썼지만 marker 지시는 지키지 않았다. 이 보조 검사 실패를 통신 실패나 Memo 공유 오류로 간주하지 않았다.

원래 자동 요약의 `core_flow_success=false`는 **A 최종 contract의 엄격한 파싱/일치까지 포함한 판정**이다. 이를 메시지 왕복 실패로 읽으면 안 된다. `A_Root_B_Root_A_actual_messages=pass`와 `accepted=false`를 함께 보고하며, 원래 결과를 성공으로 덮어쓰지 않았다.

**Qwen 실제 메시지와 처리 순서**

| KST 시각 | 실제 동작 / 입력·출력 | Tracer 근거 |
| --- | --- | --- |
| 00:36:34.916–34.927 | Root가 B/A 생성 | Root #22/#29, #24/#31 |
| 00:36:37.054 | B read_backend_contract({}) 진입, gate 닫힘 | B #21, fixture_operations |
| 00:36:39.725 | A→Root parent_ask: “Requesting the complete login API contract, including all runtime-specific fields and source_ref. Please provide the exact contract object so I can write frontend handling pseudocode that matches its structure precisely.” → qA | A #22, Root #41 |
| 00:36:42.703–42.707 | Root subagent_wait 진입 → 이미 온 qA로 해제 → Context 반영 | Root #52/#55/#59/#66 |
| 00:36:44.129 | A agent_wait_reply(qA) 시작 | A #47 |
| 00:36:48.174 | Root→B subagent_ask, related_request_id=qA → 새 qB 생성 | Root #78, B #23 |
| 00:36:48.174–48.180 | gate 해제, 도구 정상 반환, qB 적용 및 실제 SDK 요청 생성 | B #26/#28/#35 |
| 00:36:55.248 | B가 **qB가 아닌 qA**를 agent_reply의 request_id로 사용. `CommunicationError: unknown request for this task` | B #50 tool_finished failed |
| 00:36:55.481–55.483 | Root 4번째 SDK 응답이 비어 empty_response 처리, 자식 종료 요청 | Root #92–#99 |
| 00:36:55.484–55.485 | A 대기가 host_cancelled로 해제됨. pending=true, 이후 요청 종료 | A #53/#58/#59 |
| 00:37:01.646–01.652 | B의 진행 중 LLM 응답 반환 후 host_cancelled로 폐기, 자식·Root 종료 | B #68 response_discarded/#72 finished, Root #105/#108 |

Root→B의 실제 질문 본문은 다음과 같다.

> Please provide the complete login API contract object, including all runtime-specific fields and source_ref. This is needed to answer A's request (request_id=aae019696a714f918a360d148a8b6c39). Use the exact contract from read_backend_contract tool.

B의 수신 메시지에는 `request_id=3fb5f4dae9124d59a3ee9d5004bbd527` 및 `related_request_id=aae019696a714f918a360d148a8b6c39`가 구분되어 있다. 모델은 본문에 등장하는 qA를 답변 인자로 선택했다. 실패한 답변의 `message`는 Qwen fixture 전체를 담은 `{"ok":true,"output":명세}` 문자열이지만, 올바른 요청의 답변으로 수락되지 않았으므로 Root/A에 전달된 답변 수는 **0**이다.

B는 오류 뒤 3번째 LLM 요청에서 동일한 잘못된 qA 답변을 다시 생성했다. 이때는 Root 실패로 취소가 요청되어 해당 응답이 `response_discarded(reason=host_cancelled)`로 남았고 도구 실행에는 진입하지 않았다. 생성 9회와 실행 8회의 차이가 이것이다.

Root의 빈 응답은 [Qwen trace](../../../build/ollama_communication/20261017T153505Z-bee3c79e/qwen3_5_9b.trace.json)의 SDK artifact `63ac52f809c34c5190d552f3de65c4ee`에서 이미 다음 값이다.

```json
{
  "done": true,
  "done_reason": "stop",
  "prompt_eval_count": 7734,
  "eval_count": 1,
  "message": {"role": "assistant", "content": "", "thinking": null, "tool_calls": null}
}
```

이는 필요한 필드만 발췌한 것이며 전체 SDK 응답은 원본에 있다. common/normalized 응답에서 유실된 내용이 아니다. HTTP 예외·watchdog·step/tool 한도 도달은 없었다. 왜 서버/모델이 즉시 빈 응답을 반환했는지는 이 1회 관찰만으로 더 구분할 수 없다. 실패한 답변 때문에 Root가 직접 예외를 받은 것도 아니다. Root의 독립된 빈 응답이 기존 `empty_response` 종료 정책을 작동시켰다.

**실행 중 State, 안전 경계와 대기 검증**

두 모델의 B에서 같은 순서를 확인했다: `#21 tool_started < #23 agent_message_received < #26 tool_finished < #28 agent_message_applied < #35 agent_message_context_built`. 접수 순간 실시간 State는 `TaskState.status=running`, `AgentState.activity=tool_execution`, operation_kind=tool이며, qB는 incoming_request_ids와 queued_message_ids에 기록됐다. 도구 반환 기록은 completed=true/cancelled=false다. gate가 열리기 전 체류 시간은 Gemma 약 9.612초, Qwen 약 11.120초였다. 질문 때문에 기존 도구가 중단되거나 재실행되지 않았다.

Root는 두 경우 모두 `#41`에서 qA를 받았고, **그 이후** `#52`에서 subagent_wait에 진입했다. `#55`에서 incoming_request로 해제되고 `#59`에서 질문을 적용했다. 즉, 큐에 있던 질문으로 대기 진입을 빠져나오는 경로는 검증됐으며 qA가 해제 전후 incoming_request_ids에 유지됐다. 반면 “부모가 이미 기다리는 도중 새 질문 도착”과 “qB 답변 대기 중 또 다른 질문을 처리한 뒤 qB 재대기”는 두 실행 모두 발생하지 않았다. 이벤트를 주입하거나 성공으로 추정하지 않았다.

Gemma A는 실제 qA 답변 대기 중 Root의 **답변**을 받고 answered로 정상 재개했다. Root의 qB 답변은 Root가 agent_wait_reply를 호출하기 전에 도착했다. 이는 답변 도착 경로에 대한 확인이며 새 **질문**으로 대기가 해제되는 경로와 구분했다.

Gemma Root의 state history에는 waiting_for_subagents 및 waiting_for_child_reply, A에는 waiting_for_parent_reply가 있다. Qwen Root에는 waiting_for_subagents, A에는 waiting_for_parent_reply가 있다. B는 두 모델 모두 llm_call/tool_execution을 거쳤다. 최종 AgentState.activity는 여섯 Task 모두 finished이며 TaskState의 성공/실패/취소와 별개이다. Qwen은 종료 직전 Root에 qA incoming과 qB outgoing이 남아 있었고, 이후 requester_terminated/target_terminated로 닫혔다. 최종 활성 요청 배열이 비었다고 미응답 두 건을 성공으로 계산하지 않았다.

**Context, Tracer와 독립성 검증**

수락된 Gemma 메시지 4개와 Qwen 질문 2개 각각에 대해 message_id·request_id·from/to·kind·content가 수신자의 `llm_common_request`와 `llm_adapter_request.kwargs.messages`에 동일한 user 메시지로 들어갔는지 확인했다. 수신 메시지에는 direction=received가 있으며 system 지시로 승격되지 않았다. Qwen B에도 올바른 qB와 related qA가 모두 들어 있었다.

| 실제 수신 Context | common_request artifact | SDK request artifact |
| --- | --- | --- |
| Gemma Root의 qA | `5ac4990a0ef546748e6833f8ee366bae` | `fa910f58486f45aa81968e2d3fdffd53` |
| Gemma B의 qB | `d24502f921bf42a7a8b1a732e3c2c539` | `0bb5955f916847389cee60748fdbab0c` |
| Gemma Root의 B 답변 | `ea5dc9b5f4754bb1835b09540f60700f` | `43ed5dffe59847d9950540f2ac0f4c9f` |
| Gemma A의 Root 답변 | `e7dea98647c947caa3795c6db40abe04` | `b2254437ae5a40148b99d399e5183e62` |
| Qwen Root의 qA | `9ff8fe82725545488e7676dd7924154c` | `8f3d456a16db40dcbd8f1627858a84d8` |
| Qwen B의 qB | `30a987c542674cb7b2a5b417595ae03b` | `ac85ac9efd2b4b7f89362661d977b128` |

전 LLM 호출 27개의 common request → SDK request → SDK response → common response → normalized response 5개 경계를 보존했다. SDK가 공개한 thinking/content/tool_calls를 가공해 성공 응답으로 바꾸지 않았다. Gemma 공개 thinking 길이는 Root/A/B 각각 6,774/3,130/4,682자, Qwen은 3,213/527/3,080자다. 일반 content는 Gemma 모두 0자, Qwen은 A의 156자 외 0자이며 최종 산출물은 `task_complete` 인자에 있다. 공개되지 않은 내부 추론이나 서버 내부 상태를 관찰했다고 주장하지 않는다.

모든 도구의 요청 인자, 실제 결과, skipped/failed 여부, 원본 반환 artifact, LLM 출처 참조를 보존했다. 각 Task의 final_context에는 원문 Conversation과 Memo가, finished에는 최종 TaskResult가 있다. Gemma Root의 마지막 결정 Context에는 두 `subagent_result`의 실제 결과가 포함됐고, Qwen에서는 결과 회수가 수행되지 않았다.

실시간 이벤트 파일과 최종 이벤트 목록이 각 Task에서 완전히 일치했고 실시간 artifact 조회가 모두 성공했다. 관찰 State의 last_event_sequence로 최종 trace를 재생한 결과도 일치했다. 모든 Task trace와 tree의 complete/details_complete=true, JSON 저장 후 `TaskTreeSnapshot.from_json` 재읽기 및 교차 Task 참조 검증, Root TaskResult.tree와 세 session trace의 일치도 통과했다. 실패한 Qwen도 이 보존 검사를 통과했다. 단, SDK boundary projection이며 네트워크 패킷이나 서버 내부 로그의 완전성을 뜻하지 않는다.

Conversation의 assistant tool-call batch와 tool 결과 순서가 깨지지 않았고, 서로 다른 프로필의 개인 system marker가 자식 Conversation으로 복사되지 않았다. Gemma는 세 Agent가 각각 자기 task_id 소유의 Memo 1개를 만들었다. A의 개인 메모 marker는 Root가 명시적으로 subagent_result를 읽기 전에는 Root Context에 없었다. 결과 회수로 자식 요약·메모를 읽는 것은 공유 저장소와 다르다. Root/B가 marker를 저장하지 않아 **모든 Agent의 개인 Memo marker 기반 검증은 미충족**이다. Qwen은 Memo를 만들기 전에 종료했으므로 **Memo 쓰기 격리의 실제 모델 검증은 미완료**이며 빈 목록의 소유권 검사만으로 통과를 주장하지 않는다.

**실패 원인 분류와 검증 범위**

| 분류 | 관찰과 판단 |
| --- | --- |
| 1. 엔진 통신·대기 처리 | 이번에 실행된 요청 생성·접수·gate 중 실행 보존·안전 경계·큐 질문 처리·응답 전달·종료 정리에서 결함 근거를 찾지 못했다. Qwen의 잘못된 qA 답변 거절은 요청 소유권 검사와 일치한다. 관찰되지 않은 대기 경쟁 조건까지 정상이라고 일반화하지 않는다. |
| 2. LLM 도구/인자/작업 수행 | Gemma A의 task_complete 혼합 batch, 잘못된 최종 JSON과 의사코드, Root의 부정확한 산출물 검토. Qwen B의 qA/qB 혼동. 모두 실제 모델 응답에서 확인된다. |
| 3. ContextBuilder 누락 | 수락된 모든 메시지가 공통/SDK Context에 존재한다. Qwen의 올바른 qB도 존재한다. 이번 실패를 필요한 질문 누락으로 설명할 근거가 없다. |
| 4. 실제 모델/서버 | Qwen Root의 SDK 원응답이 빈 메시지였다. 환경에는 두 모델이 설치돼 있고 HTTP 예외는 없었다. 모델 생성·Ollama 템플릿·서버 내부 처리 중 세부 원인은 미확정이다. |

기존 소스의 [빈 응답 처리](../../../src/shs_agent/agent/runtime/runner.py), [요청 소유권 검사](../../../src/shs_agent/agent/session/channel.py), [부모 종료 시 자식 정리](../../../src/shs_agent/agent/subagent/manager.py)를 읽어 Tracer와 대조했으며 구현은 수정하지 않았다. 테스트 script의 watchdog과 gate는 엔진 실행 정책에 추가되지 않았다.

**원본 자료와 판정 보정의 보존**

모든 실제 실행 자료는 `build/ollama_communication/20261017T153505Z-bee3c79e/`에 있다. 실행 폴더는 새로 만들고 JSON은 배타적 생성 모드로 저장했다. API key나 민감한 환경 설정을 수집하지 않았다.

| 파일 | 내용 |
| --- | --- |
| [metadata.json](../../../build/ollama_communication/20261017T153505Z-bee3c79e/metadata.json) | 환경·모델 digest·한도·소스/실행 script 해시 |
| [summary.json](../../../build/ollama_communication/20261017T153505Z-bee3c79e/summary.json) | 실행 당시 전체 자동 판정, 두 모델 모두 accepted=false |
| [Gemma trace](../../../build/ollama_communication/20261017T153505Z-bee3c79e/gemma4_e4b.trace.json) / [result](../../../build/ollama_communication/20261017T153505Z-bee3c79e/gemma4_e4b.result.json) | 실제 TaskTree / 결과·gate 관찰 |
| [Qwen trace](../../../build/ollama_communication/20261017T153505Z-bee3c79e/qwen3_5_9b.trace.json) / [result](../../../build/ollama_communication/20261017T153505Z-bee3c79e/qwen3_5_9b.result.json) | 실패·취소를 포함한 실제 TaskTree / 결과 |
| 모델별 `.setup.json`, `.live.jsonl`, `.summary.json` | 정확한 프롬프트·fixture·실시간 이벤트·상태/검사 |
| [evidence_review.json](../../../build/ollama_communication/20261017T153505Z-bee3c79e/evidence_review.json) | 저장 자료만 재읽은 추가 검증, 시점 구분·SDK 인자 일치·source hash |
| [실행 당시 script](../../../build/ollama_communication/20261017T153505Z-bee3c79e/verify_ollama_communication.executed.py) | metadata의 script_sha256과 일치하는 원본 |
| [회귀 unittest 로그](../../../build/ollama_communication/20261017T153505Z-bee3c79e/regression_unittest.txt) | 167 tests, OK |

원래 자동 검사 `parent_wait_released_for_new_question=pass`는 incoming_request 종료 이유만 검사해 **질문이 대기 전에 왔는지** 구분하지 않았다. 실제 순서를 확인한 뒤 **검증 script만** 보완하여 `parent_wait_handles_queued_question=pass`, `parent_wait_interrupted_by_question_received_during_wait=not_observed`를 분리했다. 실행 당시 script·원본 summary/trace/result는 덮어쓰지 않고 보존했으며, 모델 재호출 없이 별도 `review_saved_run.py`로 재검토했다. 전체 실패 판정을 성공으로 바꾸지 않았다.

실제 실행 명령은 아래와 같다. 이미 이번 요청의 각 1회 실행을 마쳤으며, 이 명령을 다시 실행하면 **새로운 실험**이 된다.

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -u tests/verify_ollama_communication.py
```

회귀 확인은 실제 모델 실행 후 `.\.venv\Scripts\python.exe -m unittest discover -s tests`로 수행했다. 167개 전부 통과했고 모델 검증 script는 opt-in이므로 unittest discover에서 실제 LLM을 추가 호출하지 않는다. 오프라인 재검토 후에도 소스 해시는 동일하다. 이번 결과는 통신 왕복의 실제 성공 증거, 모델의 실패 원문, 검증하지 못한 대기 상황을 구분해 보존한 상태다.
