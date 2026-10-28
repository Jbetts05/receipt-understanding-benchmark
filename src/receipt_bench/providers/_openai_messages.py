"""Typed OpenAI chat message builders shared by the OpenAI-compatible adapters."""

from __future__ import annotations

from openai.types.chat import (
    ChatCompletionAssistantMessageParam,
    ChatCompletionContentPartImageParam,
    ChatCompletionContentPartTextParam,
    ChatCompletionMessageParam,
    ChatCompletionSystemMessageParam,
    ChatCompletionUserMessageParam,
)
from openai.types.chat.chat_completion_content_part_image_param import ImageURL


def system_message(text: str) -> ChatCompletionMessageParam:
    return ChatCompletionSystemMessageParam(role="system", content=text)


def user_text_message(text: str) -> ChatCompletionMessageParam:
    return ChatCompletionUserMessageParam(role="user", content=text)


def assistant_message(text: str) -> ChatCompletionMessageParam:
    return ChatCompletionAssistantMessageParam(role="assistant", content=text)


def user_image_message(text: str, image_url: str) -> ChatCompletionMessageParam:
    """Build a user message containing both prompt text and an image data URI."""
    text_part: ChatCompletionContentPartTextParam = {"type": "text", "text": text}
    image_part: ChatCompletionContentPartImageParam = {
        "type": "image_url",
        "image_url": ImageURL(url=image_url),
    }
    return ChatCompletionUserMessageParam(role="user", content=[text_part, image_part])
