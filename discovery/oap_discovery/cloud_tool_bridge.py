"""Cloud LLM tool bridge — routes tool calling through Claude/GPT-4.

Replaces the Ollama tool-calling loop when escalation is configured.
Discovery still finds the right manifest; this module handles the
actual tool call execution via the cloud LLM.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import httpx

from .config import EscalationConfig
from .tool_models import Tool, ToolRegistryEntry

log = logging.getLogger("oap.cloud_tool_bridge")


def _ollama_tools_to_claude(tools: list[Tool]) -> list[dict]:
    """Convert Ollama-format tool definitions to Claude's tool format."""
    claude_tools = []
    for tool in tools:
        fn = tool.function
        # Build input_schema from Ollama's parameters format
        properties = {}
        required = fn.parameters.required if fn.parameters else []
        if fn.parameters and fn.parameters.properties:
            for name, param in fn.parameters.properties.items():
                properties[name] = {
                    "type": param.type or "string",
                    "description": param.description or "",
                }
        claude_tools.append({
            "name": fn.name,
            "description": fn.description or "",
            "input_schema": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        })
    return claude_tools


def _get_api_key(cfg: EscalationConfig) -> str:
    """Resolve API key from config or environment."""
    if cfg.api_key:
        return cfg.api_key
    key = os.environ.get("OAP_ESCALATION_API_KEY", "")
    if key:
        return key
    provider_vars = {
        "anthropic": "OAP_ANTHROPIC_API_KEY",
        "openai": "OAP_OPENAI_API_KEY",
    }
    var = provider_vars.get(cfg.provider, "")
    return os.environ.get(var, "") if var else ""


async def execute_cloud_tool_loop(
    escalation_cfg: EscalationConfig,
    system_prompt: str,
    messages: list[dict[str, Any]],
    tools: list[Tool],
    registry: dict[str, ToolRegistryEntry],
    *,
    max_rounds: int = 5,
    execute_tool_fn=None,
    execute_exec_fn=None,
) -> dict[str, Any]:
    """Execute a multi-round tool-calling loop via Claude.

    Returns a result dict compatible with the Ollama tool bridge response:
    {message: {role, content}, tool_calls_made: [...], tokens_in, tokens_out}
    """
    api_key = _get_api_key(escalation_cfg)
    if not api_key:
        return {"message": {"role": "assistant", "content": "Cloud tool bridge not configured — no API key."}}

    claude_tools = _ollama_tools_to_claude(tools)

    # Convert Ollama-format messages to Claude format
    claude_messages = []
    for m in messages:
        if m.get("role") == "system":
            continue  # handled separately
        claude_messages.append({"role": m["role"], "content": m["content"]})

    base_url = escalation_cfg.base_url or "https://api.anthropic.com"
    url = f"{base_url.rstrip('/')}/v1/messages"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }

    total_tokens_in = 0
    total_tokens_out = 0
    tool_executions: list[dict] = []
    final_text = ""

    for round_num in range(max_rounds):
        payload: dict[str, Any] = {
            "model": escalation_cfg.model,
            "max_tokens": escalation_cfg.max_tokens,
            "messages": claude_messages,
            "temperature": 0.3,
        }
        if system_prompt:
            payload["system"] = system_prompt
        if claude_tools:
            payload["tools"] = claude_tools
            # Force a tool call on round 1 — if the bridge was invoked, tools
            # were discovered and action is required. Prevents Claude from
            # responding conversationally and claiming success without acting.
            if round_num == 0:
                payload["tool_choice"] = {"type": "any"}

        try:
            started = time.monotonic()
            async with httpx.AsyncClient(timeout=escalation_cfg.timeout) as client:
                resp = await client.post(url, headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()
            duration_ms = int((time.monotonic() - started) * 1000)
        except Exception as exc:
            log.error("Cloud tool bridge round %d failed: %s", round_num + 1, exc)
            break

        usage = data.get("usage", {})
        total_tokens_in += usage.get("input_tokens", 0)
        total_tokens_out += usage.get("output_tokens", 0)

        log.info(
            "Cloud tool bridge round=%d model=%s tokens_in=%d tokens_out=%d ms=%d",
            round_num + 1, escalation_cfg.model,
            usage.get("input_tokens", 0), usage.get("output_tokens", 0), duration_ms,
        )

        # Process response content blocks
        content_blocks = data.get("content", [])
        stop_reason = data.get("stop_reason", "")

        # Extract text and tool_use blocks
        text_parts = []
        tool_uses = []
        for block in content_blocks:
            if block.get("type") == "text":
                text_parts.append(block["text"])
            elif block.get("type") == "tool_use":
                tool_uses.append(block)

        if text_parts:
            final_text = " ".join(text_parts)

        # If no tool calls, we're done
        if stop_reason != "tool_use" or not tool_uses:
            break

        # Append assistant message with tool calls
        claude_messages.append({"role": "assistant", "content": content_blocks})

        # Execute each tool call
        tool_results = []
        for tool_use in tool_uses:
            tool_name = tool_use["name"]
            tool_id = tool_use["id"]
            tool_input = tool_use.get("input", {})

            log.info("Cloud tool call: %s(%s)", tool_name, json.dumps(tool_input, default=str)[:200])

            entry = registry.get(tool_name)
            result_text = ""
            exec_started = time.monotonic()

            try:
                if tool_name == "oap_exec" and execute_exec_fn:
                    result_text = await execute_exec_fn(tool_input)
                elif entry and execute_tool_fn:
                    result_text = await execute_tool_fn(entry, tool_input)
                else:
                    result_text = f"Error: unknown tool '{tool_name}'"
            except Exception as exc:
                result_text = f"Error: {exc}"

            exec_ms = int((time.monotonic() - exec_started) * 1000)
            log.info("Cloud tool result: %s → %s (%dms)", tool_name, result_text[:200], exec_ms)

            tool_executions.append({
                "tool": tool_name,
                "arguments": tool_input,
                "result": result_text,
                "duration_ms": exec_ms,
            })

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tool_id,
                "content": result_text[:50000],  # cap result size
            })

        # Send tool results back to Claude
        claude_messages.append({"role": "user", "content": tool_results})

    return {
        "message": {"role": "assistant", "content": final_text},
        "tool_executions": tool_executions,
        "tokens_in": total_tokens_in,
        "tokens_out": total_tokens_out,
    }
