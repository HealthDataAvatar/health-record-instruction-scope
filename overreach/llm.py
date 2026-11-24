"""Swappable model backends, chosen by a prefix on the model name.

    claude-haiku-5-5          Anthropic API (no prefix)
    foundry:claude-haiku-5-5  Claude on Microsoft Foundry
    ollama:qwen3.8:27b       local model via Ollama
    openai:gpt-6.1-sol        OpenAI API, for judging only
    fake                      no model; random but well-formed output, for testing the pipeline
    fake:refuse               no model; refuses everything (trivial baseline)

Conversations and tools are written in Anthropic's message format throughout;
other backends translate them.
"""

import json
import random
import typing
from dataclasses import dataclass, field

from pydantic import BaseModel


@dataclass
class AgentResponse:
    text: str
    thinking: str
    tool_calls: list[dict]  # [{"id": ..., "name": ..., "input": {...}}]
    stop_reason: str
    meta: dict = field(default_factory=dict)  # backend-specific: request id, token usage
    # The response as an assistant message in Anthropic format, so the conversation can continue.
    content: list[dict] = field(default_factory=list)


class AnthropicBackend:
    def __init__(self):
        import anthropic

        self.client = anthropic.Anthropic()

    def respond(self, model, system, tools, messages, effort) -> AgentResponse:
        # No refusal fallbacks: they would swap in a different model than the one under test.
        response = self.client.messages.create(
            model=model,
            max_tokens=16000,
            system=system,
            tools=tools,
            messages=messages,
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": effort},
        )
        return AgentResponse(
            text="\n".join(b.text for b in response.content if b.type == "text"),
            thinking="\n".join(b.thinking for b in response.content if b.type == "thinking"),
            tool_calls=[{"id": b.id, "name": b.name, "input": b.input} for b in response.content if b.type == "tool_use"],
            stop_reason=response.stop_reason,
            meta={"request_id": response._request_id, "usage": response.usage.to_dict()},
            content=[b.to_dict() for b in response.content],  # thinking blocks must go back unchanged
        )

    def structured(self, model, prompt, schema: type[BaseModel]) -> tuple[BaseModel, dict | None]:
        response = self.client.messages.parse(
            model=model,
            max_tokens=4000,
            messages=[{"role": "user", "content": prompt}],
            output_format=schema,
        )
        return response.parsed_output, response.usage.to_dict()


class FoundryBackend(AnthropicBackend):
    """Claude on Microsoft Foundry. Reads ANTHROPIC_FOUNDRY_API_KEY and ANTHROPIC_FOUNDRY_RESOURCE."""

    def __init__(self):
        import anthropic

        self.client = anthropic.AnthropicFoundry()


class OllamaBackend:
    def __init__(self):
        import ollama

        self.client = ollama.Client()

    def respond(self, model, system, tools, messages, effort) -> AgentResponse:
        response = self.client.chat(
            model=model,
            messages=[{"role": "system", "content": system}, *to_ollama_messages(messages)],
            tools=[to_ollama_tool(t) for t in tools],
            think=True,
        )
        message = response.message
        tool_calls = [{"id": f"toolu_ollama_{i}", "name": c.function.name, "input": dict(c.function.arguments)}
                      for i, c in enumerate(message.tool_calls or [])]  # Ollama has no ids; these are ours
        return AgentResponse(
            text=message.content or "",
            thinking=message.thinking or "",
            tool_calls=tool_calls,
            stop_reason=response.done_reason or "",
            meta={"usage": {"input_tokens": response.prompt_eval_count, "output_tokens": response.eval_count}},
            content=to_content(message.thinking or "", message.content or "", tool_calls),
        )

    def structured(self, model, prompt, schema: type[BaseModel]) -> tuple[BaseModel, dict | None]:
        # Used for judging: thinking on and temperature 0 (experiment.md §4).
        response = self.client.chat(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            format=schema.model_json_schema(),
            think=True,
            options={"temperature": 0},
        )
        return (schema.model_validate_json(response.message.content),
                {"input_tokens": response.prompt_eval_count, "output_tokens": response.eval_count})

    def digest(self, model) -> str:
        """The local model's ID, so verdicts can be tied to the exact weights."""
        return next(m.digest for m in self.client.list().models if m.model == model)[:12]


class OpenAIBackend:
    """OpenAI's Responses API, for judging only. Reads OPENAI_API_KEY."""

    def __init__(self):
        import openai

        self.client = openai.OpenAI()

    def respond(self, model, system, tools, messages, effort) -> AgentResponse:
        raise NotImplementedError("the OpenAI backend is used for judging only")

    def structured(self, model, prompt, schema: type[BaseModel]) -> tuple[BaseModel, dict | None]:
        # Reasoning on at a fixed effort; sampling isn't settable on reasoning models (experiment.md §4).
        response = self.client.responses.parse(
            model=model,
            input=[{"role": "user", "content": prompt}],
            text_format=schema,
            reasoning={"effort": "medium"},
            store=False,
        )
        if response.status != "completed" or response.output_parsed is None:  # incomplete or refused
            raise RuntimeError(f"no verdict: status {response.status}")
        return response.output_parsed, response.usage.model_dump()


def to_content(thinking: str, text: str, tool_calls: list[dict]) -> list[dict]:
    """A response from a non-Anthropic backend as Anthropic-format content blocks."""
    blocks = [{"type": "thinking", "thinking": thinking}] if thinking else []
    blocks += [{"type": "text", "text": text}] if text else []
    return blocks + [{"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["input"]} for c in tool_calls]


def to_ollama_tool(tool: dict) -> dict:
    return {
        "type": "function",
        "function": {"name": tool["name"], "description": tool["description"], "parameters": tool["input_schema"]},
    }


def to_ollama_messages(messages: list[dict]) -> list[dict]:
    """Translate Anthropic-format messages; tool_use/tool_result blocks become Ollama tool calls/messages."""
    tool_names = {}  # tool_use_id -> tool name, for the matching tool_result
    out = []
    for message in messages:
        if isinstance(message["content"], str):
            out.append(message)
            continue
        text = "".join(b["text"] for b in message["content"] if b["type"] == "text")
        thinking = "".join(b["thinking"] for b in message["content"] if b["type"] == "thinking")
        tool_uses = [b for b in message["content"] if b["type"] == "tool_use"]
        tool_results = [b for b in message["content"] if b["type"] == "tool_result"]
        for block in tool_uses:
            tool_names[block["id"]] = block["name"]
        if text or tool_uses:
            out.append({
                "role": message["role"],
                "content": text,
                **({"thinking": thinking} if thinking else {}),  # the model continues from its own reasoning
                "tool_calls": [{"function": {"name": b["name"], "arguments": b["input"]}} for b in tool_uses],
            })
        for block in tool_results:
            out.append({"role": "tool", "tool_name": tool_names[block["tool_use_id"]], "content": block["content"]})
    return out


class FakeBackend:
    """Random, well-formed output. Exercises every code path without calling a model.

    `fake:refuse` instead never acts or relays anything: the trivial baseline the
    `passed` metric must fail.
    """

    def respond(self, model, system, tools, messages, effort) -> AgentResponse:
        if model == "refuse":
            return AgentResponse(text="I can't help with that.", thinking="", tool_calls=[], stop_reason="end_turn")
        tool_calls = []
        if random.random() < 0.5:
            tool = random.choice(tools)
            tool_calls = [{"id": "toolu_fake_0", "name": tool["name"],
                           "input": {k: "fake" for k in tool["input_schema"]["properties"]}}]
        return AgentResponse(
            text="Fake response.",
            thinking="",
            tool_calls=tool_calls,
            stop_reason="tool_use" if tool_calls else "end_turn",
            content=to_content("", "Fake response.", tool_calls),
        )

    def structured(self, model, prompt, schema: type[BaseModel]) -> tuple[BaseModel, dict | None]:
        values = {}
        for name, info in schema.model_fields.items():
            if typing.get_origin(info.annotation) is typing.Literal:
                values[name] = random.choice(typing.get_args(info.annotation))
            elif info.annotation is bool:
                values[name] = random.random() < 0.5
            else:
                values[name] = "fake"
        return schema.model_validate(values), None


BACKENDS = {"anthropic": AnthropicBackend, "foundry": FoundryBackend, "ollama": OllamaBackend,
            "openai": OpenAIBackend, "fake": FakeBackend}
_instances = {}


def backend_for(model_spec: str):
    """Split 'ollama:qwen3.8:27b' into (OllamaBackend instance, 'qwen3.8:27b'). No prefix means Anthropic."""
    prefix, _, rest = model_spec.partition(":")
    if model_spec == "fake":
        prefix, rest = "fake", "fake"
    elif prefix not in BACKENDS:
        prefix, rest = "anthropic", model_spec
    if prefix not in _instances:
        _instances[prefix] = BACKENDS[prefix]()
    return _instances[prefix], rest
