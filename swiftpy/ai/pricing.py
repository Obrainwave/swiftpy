from __future__ import annotations

import threading

# Standard pricing table: (input_cost_per_million, output_cost_per_million) in USD
DEFAULT_MODEL_PRICING: dict[str, tuple[float, float]] = {
    # OpenAI models
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4-turbo": (10.00, 30.00),
    "gpt-4": (30.00, 60.00),
    "gpt-3.5-turbo": (0.50, 1.50),
    "o1": (15.00, 60.00),
    "o1-mini": (3.00, 12.00),
    "o3-mini": (1.10, 4.40),
    # Anthropic models
    "claude-3-5-sonnet": (3.00, 15.00),
    "claude-3-5-haiku": (0.80, 4.00),
    "claude-3-opus": (15.00, 75.00),
    "claude-3-haiku": (0.25, 1.25),
    # Embeddings (input only)
    "text-embedding-3-small": (0.02, 0.0),
    "text-embedding-3-large": (0.13, 0.0),
    "text-embedding-ada-002": (0.10, 0.0),
}

_CUSTOM_PRICING: dict[str, tuple[float, float]] = {}
_PRICING_LOCK: threading.Lock = threading.Lock()


def register_model_pricing(
    model: str,
    input_cost_per_million: float,
    output_cost_per_million: float,
) -> None:
    """
    Register custom token pricing for a model name or prefix.

    Thread-safe; cost values are specified in USD per 1,000,000 tokens.
    """
    with _PRICING_LOCK:
        _CUSTOM_PRICING[model.lower().strip()] = (
            float(input_cost_per_million),
            float(output_cost_per_million),
        )


def calculate_token_cost(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> float:
    """
    Calculate estimated dollar cost for prompt and completion token usage.

    Uses exact model match if available, otherwise checks model prefix.
    If the model is not found in custom or default pricing tables, returns 0.0.
    Negative token counts are automatically clamped to 0.

    Note: This is an estimated cost based on configured pricing rates,
    not actual provider billing.
    """
    safe_prompt = max(0, prompt_tokens)
    safe_completion = max(0, completion_tokens)
    norm_model = model.lower().strip()

    rates: tuple[float, float] | None = None
    with _PRICING_LOCK:
        custom_copy = dict(_CUSTOM_PRICING)

    # 1. Custom pricing exact match
    if norm_model in custom_copy:
        rates = custom_copy[norm_model]
    # 2. Default pricing exact match
    elif norm_model in DEFAULT_MODEL_PRICING:
        rates = DEFAULT_MODEL_PRICING[norm_model]
    else:
        # 3. Custom prefix match
        for key, val in custom_copy.items():
            if norm_model.startswith(key):
                rates = val
                break
        if rates is None:
            # 4. Default prefix match
            for key, val in DEFAULT_MODEL_PRICING.items():
                if norm_model.startswith(key):
                    rates = val
                    break

    if rates is None:
        return 0.0

    in_rate, out_rate = rates
    prompt_cost = (safe_prompt / 1_000_000.0) * in_rate
    completion_cost = (safe_completion / 1_000_000.0) * out_rate
    return round(prompt_cost + completion_cost, 6)

