"""orch-tix: mirror tickets that need you to the TIX phone app and bring decisions back."""
from .addon import TixAddon


def create(ctx) -> TixAddon:
    return TixAddon(ctx)
