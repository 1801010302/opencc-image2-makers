"""
OpenCC Image2 智能体 — EdgeOne Makers
对话式图片生成，调用 opencc.yiminju.xyz gpt-image-2
"""

from __future__ import annotations

import json
import os
import time
import traceback
from typing import AsyncGenerator

from dotenv import load_dotenv
from openai import AsyncOpenAI
from openai.types.responses import ResponseTextDeltaEvent
from openai.messages.messages import Message as OpenAIMessage

# EdgeOne injects context before the handler runs
try:
    from context import Context
    context: Context = context  # type: ignore[name-defined]
except ImportError:
    context = None

load_dotenv()

AI_GATEWAY_KEY = os.getenv("AI_GATEWAY_API_KEY", "").strip()
AI_GATEWAY_URL = os.getenv("AI_GATEWAY_BASE_URL", "https://ai-gateway.edgeone.link/v1").strip()

SYSTEM_PROMPT = """你是一个专业的 AI 图片生成助手，名为 OpenCC Image2。

你的核心能力是根据用户的描述生成高质量图片。用户可以用中文或英文描述想要的图片内容。

**你的工作方式：**
当用户描述他们想要的图片时，你调用 generate_image 工具。generate_image 工具会：
1. 调用 OpenCC 的 gpt-image-2 模型
2. 生成对应的图片
3. 返回图片保存路径

**与用户对话示例：**
- 用户："画一只在月亮上的兔子"
- 你调用 generate_image(prompt="A rabbit sitting on the moon, fantasy style, high quality")
- 返回结果后告诉用户图片已生成

**生成图片时的提示词建议：**
- 描述主体（人物/动物/物体）
- 描述环境/背景
- 描述风格（写实/插画/水彩/摄影等）
- 描述光线/氛围
- 描述尺寸/比例（方形/横版/竖版）

generate_image 工具会返回生成的图片信息，包括保存路径和图片大小。

如果用户没有指定风格，可以主动补充一个默认的高质量风格描述。

如果用户的描述太简短，可以主动补充细节，让生成的图片更精美。"""

logger = __import__("agents").get_logger("image-gen")


def sse_event(event_type: str, data: dict) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def generate_image_gen(
    message: str,
    context,
) -> AsyncGenerator[str, None]:
    """Run the image generation agent."""
    if not AI_GATEWAY_KEY:
        yield sse_event("error", {"message": "AI_GATEWAY_API_KEY is not configured"})
        return

    client = AsyncOpenAI(
        api_key=AI_GATEWAY_KEY,
        base_url=AI_GATEWAY_URL,
        timeout=120.0,
        max_retries=1,
    )

    # Import tools
    from agents._tools import generate_image

    from agents import Runner, OpenAIAgent

    agent = OpenAIAgent(tools=[generate_image], system_prompt=SYSTEM_PROMPT)
    runner = Runner(agent=agent)

    if hasattr(context, "store") and hasattr(context, "utils"):
        session = context.store.openai_session(conversation_id := context.utils.conversation_id())
    else:
        session = None
        conversation_id = "opencc-image2"

    input_items = [{"role": "user", "content": message}]

    try:
        if session:
            async with session:
                async for event in runner.run_events(session_id=conversation_id, input_items=input_items):
                    if isinstance(event, ResponseTextDeltaEvent):
                        yield sse_event("text_delta", {"delta": event.delta})
                    elif hasattr(event, "type"):
                        if event.type == "error":
                            yield sse_event("error", {"message": str(getattr(event, "message", "Unknown error"))})
                        elif event.type == "done":
                            yield sse_event("done", {})
        else:
            async for event in runner.run_events(input_items=input_items):
                if isinstance(event, ResponseTextDeltaEvent):
                    yield sse_event("text_delta", {"delta": event.delta})
                elif hasattr(event, "type"):
                    if event.type == "done":
                        yield sse_event("done", {})
    except Exception as exc:
        logger.error(f"Agent error: {exc}\n{traceback.format_exc()}")
        yield sse_event("error", {"message": str(exc)})


def sse_response(events: AsyncGenerator[str, None]) -> bytes:
    """Convert async generator to HTTP response bytes."""
    import asyncio
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    chunks = list(asyncio.run_coroutine_threadsafe(list(events), loop).result(timeout=300))
    return b"".join(c.encode() if isinstance(c, str) else c for c in chunks)


class handler:
    def __init__(self, context):
        self.context = context

    async def on_session_start(self):
        logger.log("Session started")

    async def on_message(self, message: str) -> AsyncGenerator[str, None]:
        """Main entry: receive a message from the frontend, yield SSE tokens."""
        conversation_id = getattr(self.context.utils, "conversation_id", lambda: "opencc-image2")()
        logger.log(f"on_message: conversation={conversation_id}, message={message[:100]!r}")

        try:
            async for chunk in generate_image_gen(message, self.context):
                yield chunk
        except Exception as exc:
            logger.error(f"on_message error: {exc}\n{traceback.format_exc()}")
            yield sse_event("error", {"message": str(exc)})
