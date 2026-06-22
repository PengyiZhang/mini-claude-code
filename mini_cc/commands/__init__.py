"""Slash command registry and built-in commands.

A slash command is a small user-facing action invoked from the chat
input by typing ``/`` — similar to Claude Code's slash commands or
Discord's bot commands. Examples: ``/help``, ``/clear``, ``/sessions``.

Two scopes are supported:

- ``client`` — handled entirely by the frontend (e.g. focusing the file
  tab). The backend still knows they exist so it can advertise them in
  ``GET /commands``, but their execution never round-trips to the server.
- ``server`` — executed by the backend, returning an SSE stream of the
  same event shape as ``POST /sessions/{sid}/send`` so the frontend can
  render the result exactly like a model turn.

The registry is extensible: plugins can call :meth:`CommandRegistry.register`
at startup to add their own commands without modifying this module.
"""
from __future__ import annotations

from .registry import (CommandContext, CommandRegistry, SlashCommand,
                       default_registry)

__all__ = ["CommandContext", "CommandRegistry", "SlashCommand",
           "default_registry"]
