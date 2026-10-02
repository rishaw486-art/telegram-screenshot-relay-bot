from __future__ import annotations

import base64
import logging

import httpx

from app_settings import Settings

log = logging.getLogger("groq_vision")


async def describe_image(image_bytes: bytes, settings: Settings) -> str | None:
    """Return a concise visual caption only when the operator opted into remote processing."""
    if not settings.remote_vision_enabled or not image_bytes:
        return None
    encoded = base64.b64encode(image_bytes).decode("ascii")
    endpoint = settings.vision_api_base.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {settings.vision_api_key}"}
    payload = {
        "model": settings.vision_model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "Describe the visible content of this screenshot in 1-3 concise sentences. Mention important text or controls when readable. Do not guess hidden context.",
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{encoded}"},
                    },
                ],
            }
        ],
        "max_completion_tokens": 180,
        "temperature": 0.7,
    }
    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            response = await client.post(endpoint, headers=headers, json=payload)
            response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        if isinstance(content, list):
            content = " ".join(
                item.get("text", "") for item in content if isinstance(item, dict)
            )
        text = " ".join(str(content).split())[:700]
        return text or None
    except httpx.HTTPStatusError as exc:
        log.warning("vision provider returned HTTP %s", exc.response.status_code)
        return None
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
        log.warning("vision caption failed error=%s", type(exc).__name__)
        return None
