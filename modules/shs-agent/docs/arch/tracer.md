
flowchart TD
    A["Agent A"]
    B["Agent B"]
    C["Agent C"]

    T["Tracer"]

    EVENTS[("통합 Events / Artifacts")]

    LIVE["실시간 이벤트 구독"]
    REPLAY["과거 실행 Replay"]

    SESSION["TaskSession"]
    UI["Host 시각화 UI"]

    A -->|"Event + Task ID"| T
    B -->|"Event + Task ID"| T
    C -->|"Event + Task ID"| T

    T --> EVENTS
    T --> LIVE
    EVENTS --> REPLAY

    LIVE --> SESSION
    REPLAY --> SESSION
    SESSION --> UI

    classDef object fill:#DBEAFE,stroke:#2563EB,color:#1E40AF,stroke-width:2px
    classDef action fill:#FEF3C7,stroke:#D97706,color:#92400E,stroke-width:2px
    classDef data fill:#F3E8FF,stroke:#9333EA,color:#6B21A8,stroke-width:2px
    classDef external fill:#F3F4F6,stroke:#6B7280,color:#374151

    class A,B,C,T,SESSION object
    class LIVE,REPLAY action
    class EVENTS data
    class UI external
