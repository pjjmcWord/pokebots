"""Adaptador de emulador: cliente TCP asíncrono del puente Lua (pbm_bridge.lua).

Responsabilidades:
- Conexión con reconexión automática y detección de caída.
- Validación estricta de mensajes (protocolo v1).
- Serialización de comandos: NUNCA dos secuencias de entrada incompatibles a la vez
  (un único ``asyncio.Lock`` protege el canal de entradas).
- Prioridad del control manual: mientras un humano tiene el control, las órdenes
  automáticas se rechazan en el propio adaptador.
- Parada de emergencia propagada al puente (el puente bloquea además a su nivel).

Este módulo es el ÚNICO lugar que conoce detalles del emulador; el resto de la
aplicación depende solo de la interfaz :class:`EmulatorAdapter`.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .protocol import Command, ProtocolError, parse_message

logger = logging.getLogger("pbm.emulator")

DEFAULT_HOST = "127.0.0.1"   # solo localhost (requisito de seguridad)
DEFAULT_PORT = 42070
ACK_TIMEOUT_S = 5.0
STATUS_STALE_S = 10.0        # sin status durante >10 s => conexión degradada


@dataclass
class EmulatorState:
    """Instantánea observable del emulador. Solo datos realmente leídos."""

    connected: bool = False
    rom_loaded: bool = False
    mgba_version: str | None = None
    game_code: str | None = None
    game_title: str | None = None
    paused: bool = False
    frame: int = 0
    buttons_held: list[str] = field(default_factory=list)
    last_status_ts: float = 0.0
    emergency_stop: bool = False
    manual_control_active: bool = False
    last_error: str | None = None


class EmulatorAdapter:
    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        on_event: Optional[Callable[[dict], None]] = None,
        reconnect_delay_s: float = 2.0,
    ) -> None:
        if host != "127.0.0.1":
            raise ValueError("El adaptador del emulador solo puede escuchar/conectar en localhost")
        self.host = host
        self.port = port
        self.state = EmulatorState()
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._cmd_id = 0
        self._pending_acks: dict[int, asyncio.Future] = {}
        self._input_lock = asyncio.Lock()      # exclusividad mutua de entradas
        self._read_task: asyncio.Task | None = None
        self._reconnect_task: asyncio.Task | None = None
        self._stopped = False
        self.on_event = on_event
        self.reconnect_delay_s = reconnect_delay_s

    # ------------------------------------------------------------ ciclo de vida
    async def connect(self) -> None:
        self._stopped = False
        await self._open_once()

    async def _open_once(self) -> None:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), timeout=5.0
            )
        except (OSError, asyncio.TimeoutError) as exc:
            self.state.connected = False
            self.state.last_error = f"conexión rechazada: {exc}"
            self._schedule_reconnect()
            return
        self._reader, self._writer = reader, writer
        self.state.connected = True
        self.state.last_error = None
        logger.info("Conectado al puente mGBA en %s:%s", self.host, self.port)
        self._read_task = asyncio.create_task(self._read_loop())

    def _schedule_reconnect(self) -> None:
        if self._stopped or (self._reconnect_task and not self._reconnect_task.done()):
            return

        async def _retry() -> None:
            while not self._stopped:
                await asyncio.sleep(self.reconnect_delay_s)
                if self.state.connected:
                    return
                await self._open_once()
                if self.state.connected:
                    return

        self._reconnect_task = asyncio.create_task(_retry())

    async def shutdown(self) -> None:
        self._stopped = True
        if self._read_task:
            self._read_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._read_task
        if self._writer:
            self._writer.close()
            with contextlib.suppress(Exception):
                await self._writer.wait_closed()
        self.state.connected = False

    # ------------------------------------------------------------- lectura
    async def _read_loop(self) -> None:
        assert self._reader is not None
        buffer = b""
        try:
            while True:
                chunk = await self._reader.read(4096)
                if not chunk:
                    raise ConnectionError("puente desconectado (EOF)")
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if line.strip():
                        self._handle_message(line.decode("utf-8"))
        except (ConnectionError, OSError, asyncio.IncompleteReadError) as exc:
            logger.warning("Conexión con el puente perdida: %s", exc)
            self.state.connected = False
            self.state.last_error = str(exc)
            self._fail_all_pending("conexion_perdida")
            self._schedule_reconnect()
        except asyncio.CancelledError:
            raise

    def _handle_message(self, line: str) -> None:
        try:
            msg = parse_message(line)
        except ProtocolError as exc:
            logger.error("Mensaje inválido del puente (%s); descartado", exc)
            self.state.last_error = f"mensaje_invalido: {exc}"
            return
        mtype = msg["t"]
        if mtype == "hello":
            self.state.mgba_version = msg.get("mgba_version")
            self.state.game_code = msg.get("game_code")
            self.state.game_title = msg.get("game_title")
            self.state.rom_loaded = bool(msg.get("rom_loaded"))
        elif mtype == "status":
            self.state.rom_loaded = bool(msg.get("rom_loaded"))
            self.state.paused = bool(msg.get("paused"))
            self.state.frame = int(msg.get("frame", 0))
            self.state.buttons_held = list(msg.get("buttons_held", []))
            self.state.last_status_ts = time.monotonic()
        elif mtype == "ack":
            fut = self._pending_acks.pop(int(msg["id"]), None)
            if fut and not fut.done():
                fut.set_result(msg)
        elif mtype == "memread":
            fut = self._pending_acks.pop(int(msg["req"]), None)
            if fut and not fut.done():
                fut.set_result(msg)
        elif mtype == "event":
            if msg.get("kind") == "manual_input":
                # El usuario está pulsando botones físicamente: prioridad manual.
                logger.info("Entrada manual detectada — automatizaciones en pausa")
            if self.on_event:
                self.on_event(msg)

    def _fail_all_pending(self, reason: str) -> None:
        for fut in self._pending_acks.values():
            if not fut.done():
                fut.set_exception(ConnectionError(reason))
        self._pending_acks.clear()

    # ------------------------------------------------------------- comandos
    async def send_command(self, cmd: Command, *, automated: bool = True,
                           timeout: float = ACK_TIMEOUT_S) -> dict:
        """Envía una orden y espera su ack. Serializa entradas automáticamente."""
        cmd.validate()
        if self.state.emergency_stop and cmd.action not in ("emergency_release", "ping",
                                                            "status_on", "mem_read"):
            raise PermissionError("parada de emergencia activa: no se envían entradas")
        if automated and self.state.manual_control_active:
            raise PermissionError("control manual activo: las órdenes automáticas están bloqueadas")
        if not (self.state.connected and self._writer):
            raise ConnectionError("sin conexión con el puente mGBA")

        self._cmd_id += 1
        cmd.id = self._cmd_id
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending_acks[cmd.id] = fut
        is_input_cmd = cmd.action in ("press_button", "release_button", "hold_frame")
        try:
            if is_input_cmd:
                async with self._input_lock:  # nunca dos secuencias incompatibles a la vez
                    self._writer.write((cmd.to_json() + "\n").encode("utf-8"))
                    await self._writer.drain()
                    return await asyncio.wait_for(fut, timeout)
            self._writer.write((cmd.to_json() + "\n").encode("utf-8"))
            await self._writer.drain()
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self._pending_acks.pop(cmd.id, None)
            raise TimeoutError(f"ack agotado para cmd {cmd.action} id={cmd.id}")

    # ----------------------------------------------------- API de alto nivel
    async def ping(self) -> bool:
        ack = await self.send_command(Command(id=0, action="ping"), automated=False)
        return bool(ack.get("ok"))

    async def start_status_stream(self) -> None:
        await self.send_command(Command(id=0, action="status_on"), automated=False)

    async def press_button(self, button: str, frames: int = 5, *, automated: bool = True) -> bool:
        ack = await self.send_command(
            Command(id=0, action="hold_frame", button=button, frames=frames),
            automated=automated,
        )
        return bool(ack.get("ok"))

    async def read_memory(self, region: str, address: int, width: int = 1) -> Optional[int]:
        """Lectura de memoria verificada. Devuelve None si el puente no pudo leer.

        IMPORTANTE: solo devuelve valores REALMENTE leídos del core; nunca inventa.
        """
        ack = await self.send_command(
            Command(id=0, action="mem_read", region=region, address=address, width=width),
            automated=False,
        )
        if ack.get("error"):
            logger.debug("lectura de memoria falló: %s", ack["error"])
            return None
        value = ack.get("value")
        return int(value) if value is not None else None

    async def emergency_stop(self) -> None:
        """Detiene TODAS las entradas automáticas (en el backend y en el puente)."""
        self.state.emergency_stop = True
        try:
            await self.send_command(Command(id=0, action="emergency_stop"), automated=False)
        except (ConnectionError, TimeoutError) as exc:
            logger.error("parada de emergencia enviada sin ack (estado local igualmente activo): %s", exc)

    async def emergency_release(self) -> None:
        ack = await self.send_command(Command(id=0, action="emergency_release"), automated=False)
        if ack.get("ok"):
            self.state.emergency_stop = False

    def acquire_manual_control(self) -> None:
        """El usuario toma el control: la automatización queda bloqueada ya."""
        self.state.manual_control_active = True

    def release_manual_control(self) -> None:
        """Devuelve el control al sistema: se exigirá releer estado antes de reanudar."""
        self.state.manual_control_active = False

    @property
    def status_is_fresh(self) -> bool:
        return (self.state.connected
                and (time.monotonic() - self.state.last_status_ts) < STATUS_STALE_S)
