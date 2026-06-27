"""
llm_client.py
-------------
Thin abstraction over Claude and Gemini APIs.
All other modules call call_llm() — they never import anthropic or google directly.
"""

from typing import Optional


def call_llm(
    config: dict,
    system_prompt: str,
    messages: list[dict],
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
) -> str:
    """
    Send a chat request to whichever provider is set in config.

    Args:
        config: full config dict loaded from config.json
        system_prompt: the system/instruction prompt
        messages: list of {"role": "user"|"assistant", "content": str}
        temperature: overrides config default if provided
        max_tokens: overrides config default if provided

    Returns:
        The model's response as a plain string.

    Raises:
        ValueError: if provider is not recognised
        RuntimeError: if the API call fails
    """
    provider = config.get("api_provider", "claude").lower()

    temp = temperature if temperature is not None else config["negotiation"]["temperature"]
    tokens = max_tokens if max_tokens is not None else config[provider]["max_tokens"]

    if provider == "claude":
        return _call_claude(config, system_prompt, messages, temp, tokens)
    elif provider == "gemini":
        return _call_gemini(config, system_prompt, messages, temp, tokens)
    else:
        raise ValueError(f"Unknown api_provider '{provider}'. Use 'claude' or 'gemini'.")


# ── Claude ───────────────────────────────────────────────────────────────────

def _call_claude(
    config: dict,
    system_prompt: str,
    messages: list[dict],
    temperature: float,
    max_tokens: int,
) -> str:
    try:
        import anthropic
    except ImportError:
        raise RuntimeError(
            "anthropic package not installed. Run: pip install anthropic"
        )

    client = anthropic.Anthropic(api_key=config["claude"]["api_key"])

    response = client.messages.create(
        model=config["claude"]["model"],
        max_tokens=max_tokens,
        system=system_prompt,
        messages=messages,
        temperature=temperature,
    )

    return response.content[0].text


# ── Gemini ───────────────────────────────────────────────────────────────────

def _call_gemini(
    config: dict,
    system_prompt: str,
    messages: list[dict],
    temperature: float,
    max_tokens: int,
) -> str:
    try:
        import google.generativeai as genai
    except ImportError:
        raise RuntimeError(
            "google-generativeai package not installed. "
            "Run: pip install google-generativeai"
        )

    genai.configure(api_key=config["gemini"]["api_key"])

    generation_config = genai.types.GenerationConfig(
        temperature=temperature,
        max_output_tokens=max_tokens,
    )

    model = genai.GenerativeModel(
        model_name=config["gemini"]["model"],
        system_instruction=system_prompt,
        generation_config=generation_config,
    )

    # Gemini uses "model" / "user" roles (not "assistant")
    history = []
    for msg in messages[:-1]:
        role = "model" if msg["role"] == "assistant" else "user"
        history.append({"role": role, "parts": [msg["content"]]})

    chat = model.start_chat(history=history)
    last = messages[-1]["content"]
    response = chat.send_message(last)

    return response.text
