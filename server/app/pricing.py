from app.tables import Model


def cost_for(model: Model, input_tokens: int, output_tokens: int) -> float:
    """USD cost from the model's per-1M-token prices (configured on the backend)."""
    return (input_tokens * (model.input_price or 0) + output_tokens * (model.output_price or 0)) / 1_000_000
