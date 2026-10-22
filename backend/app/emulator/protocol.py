"""Protocolo PBM v1: definición y validación de mensajes JSON-line.

Única fuente de verdad documental: ``shared/schemas/protocol.schema.json``.
Este módulo es la implementación Python equivalente (el lado Lua mantiene su
propio codificador/decodificador mínimo).

Cada mensaje es una línea JSON UTF-8 terminada en ``\\n``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

PROTOCOL_VERSION = 1

BUTTONS: frozenset[str] = frozenset(
    {"A", "B", "SELECT", "START", "RIGHT", "LEFT", "UP", "DOWN", "R", "L"}
)

MEMORY_REGIONS: frozenset[str] = frozenset(
    {"IWRAM", "EWRAM", "ROM", "PALETTE", "VRAM", "OAM", "SRAM", "FLASH"}
)

CMD_ACTIONS: frozenset[str] = frozenset(
    {
        "ping",
        "status_on",
        "press_button",
        "release_button",
        "hold_frame",
        "emergency_stop",
        "emergency_release",
        "mem_read",
        "set_speed",
    }
)


class ProtocolError(ValueError):
    """Mensaje no conforme al protocolo PBM v1."""


@dataclass(slots=True)
class Command:
    """Orden del backend hacia el puente Lua."""

    id: int
    action: str
    button: str | None = None
    frames: int | None = None
    region: str | None = None
    address: int | None = None
    width: int | None = None
    hz: int | None = None

    def to_json(self) -> str:
        payload: dict[str, Any] = {"v": PROTOCOL_VERSION, "t": "cmd", "id": self.id,
                                   "action": self.action}
        for name in ("button", "frames", "region", "address", "width", "hz"):
            value = getattr(self, name)
            if value is not None:
                payload[name] = value
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def validate(self) -> None:
        if self.action not in CMD_ACTIONS:
            raise ProtocolError(f"acción desconocida: {self.action!r}")
        if self.action in ("press_button", "release_button", "hold_frame"):
            if self.button not in BUTTONS:
                raise ProtocolError(f"botón inválido para {self.action}: {self.button!r}")
        if self.action == "hold_frame":
            if self.frames is not None and not (0 <= self.frames <= 600):
                raise ProtocolError("frames fuera de rango [0, 600]")
        if self.action == "mem_read":
            if self.region not in MEMORY_REGIONS:
                raise ProtocolError(f"región de memoria inválida: {self.region!r}")
            if self.width not in (1, 2, 4):
                raise ProtocolError("width debe ser 1, 2 o 4")
            if self.address is None or self.address < 0:
                raise ProtocolError("address inválida")


def parse_message(line: str) -> dict[str, Any]:
    """Valida y devuelve un mensaje del puente (hello/status/ack/memread/event).

    Lanza :class:`ProtocolError` si la línea no cumple el protocolo v1.
    """
    line = line.strip()
    if not line:
        raise ProtocolError("línea vacía")
    try:
        msg = json.loads(line)
    except json.JSONDecodeError as exc:  # pragma: no cover - propagado como ProtocolError
        raise ProtocolError(f"JSON inválido: {exc}") from exc
    if not isinstance(msg, dict):
        raise ProtocolError("el mensaje debe ser un objeto JSON")
    if msg.get("v") != PROTOCOL_VERSION:
        raise ProtocolError(f"versión de protocolo incompatible: {msg.get('v')!r}")
    mtype = msg.get("t")
    if mtype not in ("hello", "status", "ack", "memread", "event"):
        raise ProtocolError(f"tipo de mensaje desconocido: {mtype!r}")
    if mtype == "hello":
        if "rom_loaded" not in msg or "mgba_version" not in msg:
            raise ProtocolError("hello incompleto")
    elif mtype == "status":
        if "seq" not in msg or "rom_loaded" not in msg:
            raise ProtocolError("status incompleto")
    elif mtype == "ack":
        for key in ("id", "ok"):
            if key not in msg:
                raise ProtocolError(f"ack sin campo obligatorio {key!r}")
    elif mtype == "memread":
        for key in ("req", "region", "address", "width"):
            if key not in msg:
                raise ProtocolError(f"memread sin campo obligatorio {key!r}")
    elif mtype == "event":
        if "kind" not in msg:
            raise ProtocolError("event sin kind")
    return msg


def encode_line(msg: dict[str, Any]) -> bytes:
    """Serializa un mensaje a bytes con terminador \\n (para enviar al puente)."""
    return (json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
