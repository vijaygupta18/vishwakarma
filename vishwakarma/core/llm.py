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

# Cap LiteLLM's internal retry backoff — don't let it wait 60s between retries
litellm.num_retries = 2              # max 2 retries per call (not infinite)
litellm.request_timeout = 30         # 30s per attempt


class LLMConfig(BaseModel):
    model: str
    fast_model: str | None = None  # cheap/fast model for summarization + compaction
    # Fallback chains — tried in order, first success wins
    fast_fallbacks: list[str] = []  # e.g. ["openai/kimi-latest", "openai/glm-flash-experimental"]
    model_fallbacks: list[str] = []  # e.g. ["openai/glm-latest"]
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

    def _call_with_fallback(
        self,
        models: list[str],
        messages: list[dict],
        max_tokens: int = 4096,
        temperature: float = 0.0,
        timeout: int = 30,
        tools: list | None = None,
        total_budget: int = 60,
    ):
        """Try models in order, return first successful response.

        Each model gets `timeout` seconds. Total time across all models
        capped at `total_budget` seconds. Raises the last exception if all fail.
        """
        start = time.time()
        last_error = None
        for i, model in enumerate(models):
            # Check total time budget
            elapsed = time.time() - start
            if elapsed > total_budget:
                log.warning(f"Fallback chain exhausted time budget ({total_budget}s)")
                break
            remaining = min(timeout, int(total_budget - elapsed))
            if remaining < 5:
                break  # not enough time for another attempt

            try:
                kwargs: dict[str, Any] = {
                    "model": model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "timeout": remaining,
                    "num_retries": 1,  # max 1 retry per model in the chain
                }
                if self.cfg.api_key:
                    kwargs["api_key"] = self.cfg.api_key
                if self.cfg.api_base:
                    kwargs["api_base"] = self.cfg.api_base
                if tools:
                    kwargs["tools"] = tools
                # Disable reasoning for fast calls (summarize, compress)
                # Works for GLM-5 (enable_thinking) and Kimi-K2.5 (thinking)
                if not tools:
                    kwargs["extra_body"] = {
                        "chat_template_kwargs": {"enable_thinking": False, "thinking": False}
                    }
                response = completion(**kwargs)
                if i > 0:
                    log.info(f"Fallback to {model} succeeded (primary failed)")
                return response
            except Exception as e:
                last_error = e
                error_type = type(e).__name__
                # Rate limit: extract reset time and wait briefly before next attempt
                if "RateLimit" in error_type or "429" in str(e):
                    import re
                    reset_match = re.search(r'resets at: (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', str(e))
                    if reset_match:
                        from datetime import datetime, timezone
                        try:
                            reset_time = datetime.strptime(reset_match.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                            wait_secs = (reset_time - datetime.now(timezone.utc)).total_seconds()
                            if 0 < wait_secs <= 10:  # only wait if reset is within 10s
                                log.info(f"Rate limit resets in {wait_secs:.0f}s — waiting")
                                time.sleep(min(wait_secs + 0.5, 10))
                                continue  # retry same model after rate limit reset
                        except Exception:
                            pass
                log.warning(f"Model {model} failed ({error_type}: {str(e)[:80]}), "
                           f"{'trying next' if i < len(models) - 1 else 'no more fallbacks'} "
                           f"[{time.time() - start:.1f}s elapsed]")
        raise last_error  # type: ignore

    def _get_fast_chain(self) -> list[str]:
        """Get ordered list of fast models to try."""
        primary = self.cfg.fast_model or self.cfg.model
        fallbacks = self.cfg.fast_fallbacks or []
        chain = [primary] + [f for f in fallbacks if f != primary]
        # Always include main model as last resort
        if self.cfg.model not in chain:
            chain.append(self.cfg.model)
        return chain

    def _get_main_chain(self) -> list[str]:
        """Get ordered list of main models to try."""
        chain = [self.cfg.model] + (self.cfg.model_fallbacks or [])
        return chain

    def summarize(self, prompt: str) -> str:
        """
        Fast, cheap LLM call to compress a long tool output.
        Uses fast model chain with fallbacks.
        """
        try:
            response = self._call_with_fallback(
                models=self._get_fast_chain(),
                messages=[{"role": "user", "content": prompt}],
                max_tokens=4096,
                timeout=30,  # fast calls should be fast — 30s timeout per model
            )
            msg = response.choices[0].message
            content = msg.content or ""
            # Reasoning models may put content in reasoning_content
            if not content.strip():
                content = getattr(msg, "reasoning_content", "") or ""
            # Strip reasoning preamble if present
            import re
            content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
            return content or prompt[:2000]
        except Exception as e:
            log.warning(f"All summarization models failed: {e} — truncating instead")
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
