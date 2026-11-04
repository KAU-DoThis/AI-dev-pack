
flowchart TD
    RT["TaskRuntime"]

    subgraph LOOP["TaskRuntime - Agent Loop"]
        INPUT["대기 입력 및 명령 적용"]
        LIMIT{"실행 가능한 상태인가?"}

        BUILDER["ContextBuilder"]
        BUILD["Step Context 구성"]
        CORE["AgentCore"]
        LLM["LLM Provider"]
        RESPONSE{"LLM 호출 결과"}

        VALID{"응답 적용 가능한가?"}
        SAVE["LLM 응답 Conversation 기록"]
        ACTION{"다음 행동"}

        COMPACT["Compaction 수행"]

        DISPATCH["Tool Calls 실행 요청"]
        RECORD["Tool Result Conversation 기록"]
        OUTCOME{"Tool 실행 결과 처리"}

        WAIT["응답 또는 작업 결과 대기"]
        COMPLETE["task_complete 검증"]
        FINISH["TaskResult 생성"]
        STOP["취소 / 한도 / 복구 불가 오류 종료"]

        STATE_UPDATE["현재 State 갱신 요청"]

        %% 실행 시작 및 한도 확인
        INPUT --> LIMIT
        LIMIT -->|"Yes"| BUILDER
        LIMIT -->|"No"| STOP

        %% Context 구성 및 LLM 호출
        BUILDER --> BUILD
        BUILD --> CORE
        CORE --> LLM
        LLM --> RESPONSE

        %% LLM 응답 처리
        RESPONSE -->|"정상"| VALID
        RESPONSE -->|"Context 초과"| COMPACT
        COMPACT --> INPUT
        RESPONSE -->|"기타 오류"| STOP

        VALID -->|"Yes"| SAVE
        VALID -->|"무효"| INPUT

        %% 행동 결정
        SAVE --> ACTION
        ACTION -->|"Tool Calls"| DISPATCH
        ACTION -->|"중간 텍스트"| INPUT

        %% Tool System 호출과 결과 수신
        DISPATCH -->|"Tool System 실행 / 결과 수신"| RECORD
        RECORD --> OUTCOME

        OUTCOME -->|"다음 판단"| INPUT
        OUTCOME -->|"대기 필요"| WAIT
        OUTCOME -->|"완료 요청"| COMPLETE

        %% 대기 및 작업 재개
        WAIT -->|"답변 / 결과 / 새 요청"| INPUT

        %% 작업 완료 및 종료
        COMPLETE -->|"유효"| FINISH
        COMPLETE -->|"무효"| INPUT
        STOP --> FINISH

        %% State 변경 요청
        INPUT -.-> STATE_UPDATE
        CORE -.-> STATE_UPDATE
        DISPATCH -.-> STATE_UPDATE
        WAIT -.-> STATE_UPDATE
        COMPACT -.-> STATE_UPDATE
        FINISH -.-> STATE_UPDATE
    end

    RT --> INPUT

    %% =========================
    %% 스타일
    %% =========================
    classDef external fill:#F3F4F6,stroke:#6B7280,color:#374151,stroke-width:2px
    classDef object fill:#DBEAFE,stroke:#2563EB,color:#1E40AF,stroke-width:2px
    classDef action fill:#FEF3C7,stroke:#D97706,color:#92400E,stroke-width:2px
    classDef decision fill:#FCE7F3,stroke:#DB2777,color:#9D174D,stroke-width:2px

    class LLM external
    class RT,BUILDER,CORE object
    class INPUT,BUILD,SAVE,COMPACT,DISPATCH,RECORD,WAIT,COMPLETE,FINISH,STOP,STATE_UPDATE action
    class LIMIT,RESPONSE,VALID,ACTION,OUTCOME decision
