"""
LLM abstraction layer — wraps LiteLLM for multi-provider support.

Supports:
  - OpenAI (GPT-4, GPT-4o, etc.)
  - Anthropic (Claude)
  - Azure OpenAI
  - Juspay AI (OpenAI-compatible custom endpoint)
  - Any OpenAI-compatible provider via api_base
"""
import json
import logging
import os
import time
from typing import Any, Generator

import litellm
from litellm import completion, completion_cost
from pydantic import BaseModel

from vishwakarma.core.models import InvestigationMeta

log = logging.getLogger(__name__)

# Suppress LiteLLM's verbose logging
litellm.suppress_debug_info = True
os.environ.setdefault("LITELLM_LOG", "ERROR")


class LLMConfig(BaseModel):
    model: str
    fast_model: str | None = None  # cheap/fast model for summarization + compaction
    api_key: str | None = None
    api_base: str | None = None
    api_version: str | None = None
    max_tokens: int = 65536
    temperature: float = 0.0
    timeout: int = 300


class LLMResponse(BaseModel):
    content: str
    tool_calls: list[dict] = []
    raw: dict = {}
    cost: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0


class VishwakarmaLLM:
    """
    Main LLM client for Vishwakarma.
    Wraps LiteLLM to support all providers with one interface.
    """

    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        self._total_cost = 0.0
        self._total_prompt_tokens = 0
        self._total_completion_tokens = 0

    def complete(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        response_format: dict | None = None,
        stream: bool = False,
    ) -> LLMResponse:
        """
        Call the LLM with messages and optional tools.
        Returns structured LLMResponse.
        """
        kwargs: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": messages,
            "temperature": self.cfg.temperature,
            "timeout": self.cfg.timeout,
        }

        if self.cfg.api_key:
            kwargs["api_key"] = self.cfg.api_key
        if self.cfg.api_base:
            kwargs["api_base"] = self.cfg.api_base
        if self.cfg.api_version:
            kwargs["api_version"] = self.cfg.api_version
        if self.cfg.max_tokens:
            kwargs["max_tokens"] = self.cfg.max_tokens
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if response_format:
            kwargs["response_format"] = response_format

        # Env var overrides for custom providers (e.g. Juspay)
        max_content = os.environ.get("OVERRIDE_MAX_CONTENT_SIZE")
        max_output = os.environ.get("OVERRIDE_MAX_OUTPUT_TOKEN")
        if max_content:
            litellm.max_input_tokens = int(max_content)  # type: ignore
        if max_output:
            kwargs["max_tokens"] = int(max_output)

        try:
            response = completion(**kwargs)
            return self._parse_response(response)
        except litellm.exceptions.RateLimitError:
            raise
        except litellm.exceptions.AuthenticationError:
            raise
        except Exception as e:
            log.error(f"LLM call failed: {e}", exc_info=True)
            raise

    def stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> Generator[dict, None, None]:
        """
        Stream LLM response events.
        Yields dicts with type: text_delta | tool_call_delta | done
        """
        kwargs: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": messages,
            "temperature": self.cfg.temperature,
            "timeout": self.cfg.timeout,
            "stream": True,
        }
        if self.cfg.api_key:
            kwargs["api_key"] = self.cfg.api_key
        if self.cfg.api_base:
            kwargs["api_base"] = self.cfg.api_base
        if self.cfg.api_version:
            kwargs["api_version"] = self.cfg.api_version
        if self.cfg.max_tokens:
            kwargs["max_tokens"] = self.cfg.max_tokens
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        # Apply env var overrides (same as complete())
        max_content = os.environ.get("OVERRIDE_MAX_CONTENT_SIZE")
        max_output = os.environ.get("OVERRIDE_MAX_OUTPUT_TOKEN")
        if max_content:
            litellm.max_input_tokens = int(max_content)  # type: ignore
        if max_output:
            kwargs["max_tokens"] = int(max_output)

        try:
            response = completion(**kwargs)
        except Exception as e:
            log.error(f"LLM stream call failed: {e}", exc_info=True)
            raise

        collected_content = ""
        collected_tool_calls: dict[int, dict] = {}

        for chunk in response:
            delta = chunk.choices[0].delta if chunk.choices else None
            if not delta:
                continue

            # Text delta
            if delta.content:
                collected_content += delta.content
                yield {"type": "text_delta", "content": delta.content}

            # Tool call delta
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in collected_tool_calls:
                        collected_tool_calls[idx] = {
                            "id": tc.id or "",
                            "type": "function",
                            "function": {"name": "", "arguments": ""},
                        }
                    if tc.function:
                        if tc.function.name:
                            collected_tool_calls[idx]["function"]["name"] += tc.function.name
                        if tc.function.arguments:
                            collected_tool_calls[idx]["function"]["arguments"] += tc.function.arguments

        # Emit complete tool calls
        tool_calls = list(collected_tool_calls.values())
        if tool_calls:
            yield {"type": "tool_calls", "tool_calls": tool_calls, "content": collected_content}
        else:
            yield {"type": "analysis_done", "content": collected_content}

    def _parse_response(self, response) -> LLMResponse:
        choice = response.choices[0]
        message = choice.message

        content = message.content or ""
        tool_calls = []

        if message.tool_calls:
            for tc in message.tool_calls:
                try:
                    args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                except json.JSONDecodeError:
                    args = {"raw": tc.function.arguments}
                tool_calls.append({
                    "id": tc.id,
                    "name": tc.function.name,
                    "params": args,
                })

        # Cost tracking
        cost = 0.0
        prompt_tokens = 0
        completion_tokens = 0
        cached_tokens = 0

        if hasattr(response, "usage") and response.usage:
            usage = response.usage
            prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
            completion_tokens = getattr(usage, "completion_tokens", 0) or 0
            cached_tokens = getattr(
                getattr(usage, "prompt_tokens_details", None),
                "cached_tokens", 0
            ) or 0
            try:
                cost = completion_cost(completion_response=response)
            except Exception:
                cost = 0.0

        self._total_cost += cost
        self._total_prompt_tokens += prompt_tokens
        self._total_completion_tokens += completion_tokens

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            cost=cost,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cached_tokens=cached_tokens,
        )

    def summarize(self, prompt: str) -> str:
        """
        Fast, cheap LLM call to compress a long tool output.
        Uses fast_model if configured (open-fast), otherwise falls back to main model.
        """
        model = self.cfg.fast_model or self.cfg.model
        try:
            response = completion(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=4096,   # 1024 was too small for compaction summaries
                timeout=60,
                **({"api_key": self.cfg.api_key} if self.cfg.api_key else {}),
                **({"api_base": self.cfg.api_base} if self.cfg.api_base else {}),
            )
            return response.choices[0].message.content or prompt[:2000]
        except Exception as e:
            log.warning(f"Summarization failed: {e} — truncating instead")
            return prompt[:4000] + "\n... [truncated]"

    def build_meta(self, steps: int, compactions: int, start_time: float) -> InvestigationMeta:
        return InvestigationMeta(
            model=self.cfg.model,
            total_cost=round(self._total_cost, 6),
            prompt_tokens=self._total_prompt_tokens,
            completion_tokens=self._total_completion_tokens,
            steps_taken=steps,
            compactions=compactions,
            duration_seconds=round(time.time() - start_time, 2),
        )
