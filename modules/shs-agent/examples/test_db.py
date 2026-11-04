
import asyncio

from shs_agent.storage import (
    SQLiteStorage,
    TaskRecord,
    StorageRecord,
)
from shs_agent.storage.provider.postgresql import PostgreSQLStorage


async def main():
    storage = SQLiteStorage()
    #storage = PostgreSQLStorage(
    #    "postgresql://user:password@localhost:5432/agent"
    #)

    try:
        await storage.initialize()

        await storage.create_task(
            TaskRecord(task_id="task-001")
        )

        message = StorageRecord(
            task_id="task-001",
            kind="message",
            message_index=0,
            payload={
                "role": "user",
                "content": "프로젝트 구조를 분석해줘.",
            },
        )

        await storage.append(message)

        original = await storage.get(
            "default",
            "task-001",
            message.record_id,
        )
        print(original.payload)

    finally:
        await storage.close()


asyncio.run(main())
