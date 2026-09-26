"""Bidirectional channels subsystem.

Public surface:
- ``Channel`` protocol + ``ChannelBinding`` dataclass
- ``ChannelRegistry`` per-project persistence
- ``ChannelDispatcher`` outbound fan-out
- ``ChannelReceiverSupervisor`` long-lived receiver pool (WS mode)
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
    supports_transport,
)
from .inbound import enqueue_inbound_turn
from .supervisor import (
    ChannelReceiverSupervisor,
    get_supervisor,
    reset_for_test,
)


def _ensure_feishu_loaded() -> None:
    """Import the Feishu modules so ``register_channel_kind`` runs for
    both webhook + ws modes.

    Called lazily from the server's startup hook — keeps the cost off
    the import path of unrelated code paths. ``feishu`` self-registers
    with ``supported_transports=("ws","webhook")``; its factory
    dispatches to ``FeishuWsChannel`` when ``binding.transport=="ws"``.
    """
    from . import feishu  # noqa: F401


__all__ = [
    "Channel",
    "ChannelBinding",
    "ChannelDispatcher",
    "ChannelReceiverSupervisor",
    "ChannelRegistry",
    "InboundResult",
    "enqueue_inbound_turn",
    "get_supervisor",
    "register_channel_kind",
    "registered_kinds",
    "reset_for_test",
    "supports_transport",
    "_ensure_feishu_loaded",
]
