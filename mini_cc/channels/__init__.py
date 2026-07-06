"""Bidirectional channels subsystem.

Public surface:
- ``Channel`` protocol + ``ChannelBinding`` dataclass
- ``ChannelRegistry`` per-project persistence
- ``ChannelDispatcher`` outbound fan-out
- ``InboundResult`` inbound-handshake return type
- ``register_channel_kind`` for transports to self-register
"""
from .base import (
    Channel,
    ChannelBinding,
    ChannelDispatcher,
    ChannelRegistry,
    InboundResult,
    register_channel_kind,
    registered_kinds,
)


def _ensure_feishu_loaded() -> None:
    """Import the Feishu module so its ``register_channel_kind`` runs.

    Called lazily from the server's startup hook — keeps the cost off
    the import path of unrelated code paths.
    """
    from . import feishu  # noqa: F401


__all__ = [
    "Channel",
    "ChannelBinding",
    "ChannelDispatcher",
    "ChannelRegistry",
    "InboundResult",
    "register_channel_kind",
    "registered_kinds",
    "_ensure_feishu_loaded",
]
