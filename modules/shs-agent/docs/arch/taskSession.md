
flowchart TD
    %% ===========================
    %% 이것은 agent interrupt 흐름
    %% ===========================
    USER["User"]
    HOST["Host"]
    SESSION["TaskSession"]
    SEND["메시지 전달"]
    CHANNEL["TaskChannel"]
    QUEUE[("Input Queue")]
    RUNTIME["TaskRuntime"]
    APPLY["다음 실행 경계에서 메시지 적용"]
    CONTINUE["Agent Loop 계속 실행"]

    USER -->|"추가 지시 / 질문 답변"| HOST
    HOST -->|"Session API 호출"| SESSION
    SESSION --> SEND
    SEND --> CHANNEL
    CHANNEL --> QUEUE
    QUEUE -->|"메시지 수신"| RUNTIME
    RUNTIME --> APPLY
    APPLY --> CONTINUE

    classDef external fill:#F3F4F6,stroke:#6B7280,color:#374151
    classDef object fill:#DBEAFE,stroke:#2563EB,color:#1E40AF,stroke-width:2px
    classDef action fill:#FEF3C7,stroke:#D97706,color:#92400E
    classDef data fill:#F3E8FF,stroke:#9333EA,color:#6B21A8

    class USER,HOST external
    class SESSION,CHANNEL,RUNTIME object
    class SEND,APPLY,CONTINUE action
    class QUEUE data
