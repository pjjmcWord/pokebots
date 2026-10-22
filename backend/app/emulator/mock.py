"""Simulador del puente mGBA (mock) — implementa el protocolo PBM v1 sobre TCP.

Permite ejecutar las pruebas de integración sin emulador ni ROM ni GUI.
Los datos que produce están MARCADOS como simulados; nunca se presentan como
estado real del juego (requisito del proyecto).
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from .protocol import PROTOCOL_VERSION, parse_message


class MockBridgeServer:
    """Servidor TCP que se comporta como pbm_bridge.lua para las pruebas."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        if host != "127.0.0.1":
            raise ValueError("el mock solo escucha en localhost")
        self.host = host
        self.port = port
        self.server: asyncio.Server | None = None
        self.emergency_stop = False
        self.rom_loaded = True
        self.held_buttons: set[str] = set()
        self.seq = 0
        self.clients: list[asyncio.StreamWriter] = []
        self.received_cmds: list[dict] = []

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._handle_client, self.host, self.port)
        assert self.server.sockets
        return self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        for c in self.clients:
            c.close()
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    async def _send(self, msg: dict[str, Any]) -> None:
        data = (json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        for c in list(self.clients):
            try:
                c.write(data)
                await c.drain()
            except Exception:
                self.clients.remove(c)

    async def _handle_client(self, reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter) -> None:
        self.clients.append(writer)
        await self._send({
            "v": PROTOCOL_VERSION, "t": "hello",
            "mgba_version": "mock-0.10 (SIMULADO)",
            "game_code": "BPRE", "game_title": "POKEMON RED FIRE (SIMULADO)",
            "rom_loaded": self.rom_loaded,
        })
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                text = line.decode("utf-8").strip()
                if not text:
                    continue
                try:
                    msg = json.loads(text)
                except json.JSONDecodeError as exc:
                    await self._send({"v": 1, "t": "ack", "id": 0, "ok": False,
                                      "reason": f"mensaje_invalido: {exc}"})
                    continue
                if msg.get("v") != PROTOCOL_VERSION:
                    await self._send({"v": 1, "t": "ack", "id": msg.get("id", 0),
                                      "ok": False, "reason": "version_incompatible"})
                    continue
                if msg.get("t") == "cmd":
                    await self.handle_cmd_line(text)
        except (asyncio.CancelledError, ConnectionError):
            pass
        finally:
            if writer in self.clients:
                self.clients.remove(writer)

    async def handle_cmd_line(self, line: str) -> None:
        """Procesa una línea cmd y responde ack/memread (usado por el eco-loop)."""
        msg = json.loads(line)
        if msg.get("t") != "cmd":
            return
        self.received_cmds.append(msg)
        cid = msg.get("id", 0)
        action = msg.get("action")
        if action == "emergency_stop":
            self.emergency_stop = True
            self.held_buttons.clear()
            await self._send({"v": 1, "t": "ack", "id": cid, "ok": True,
                              "reason": "parada de emergencia activa"})
            return
        if action == "emergency_release":
            self.emergency_stop = False
            await self._send({"v": 1, "t": "ack", "id": cid, "ok": True})
            return
        if self.emergency_stop and action in ("press_button", "release_button", "hold_frame"):
            await self._send({"v": 1, "t": "ack", "id": cid, "ok": False,
                              "reason": "parada_emergencia"})
            return
        if action == "ping" or action == "status_on":
            await self._send({"v": 1, "t": "ack", "id": cid, "ok": True})
        elif action in ("press_button", "hold_frame"):
            self.held_buttons.add(msg["button"])
            await self._send({"v": 1, "t": "ack", "id": cid, "ok": True})
        elif action == "release_button":
            self.held_buttons.discard(msg["button"])
            await self._send({"v": 1, "t": "ack", "id": cid, "ok": True})
        elif action == "mem_read":
            addr, region = int(msg["address"]), msg["region"]
            # Valores deterministas falsos pero claramente marcados por convención:
            value = (addr % 256) if region in ("IWRAM", "EWRAM", "SRAM", "ROM") else None
            err = None if value is not None else "lectura_fuera_de_rango"
            await self._send({"v": 1, "t": "memread", "req": cid, "region": region,
                              "address": addr, "width": msg["width"],
                              "value": value, "error": err})
        else:
            await self._send({"v": 1, "t": "ack", "id": cid, "ok": False,
                              "reason": "accion_desconocida"})
