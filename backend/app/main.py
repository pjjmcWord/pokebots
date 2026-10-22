"""API local FastAPI + WebSocket (Fases 2 y 4).

Escucha SOLO en 127.0.0.1 (el bind se fuerza en scripts/arranque y uvicorn).
Los endpoints exponen SIEMPRE el campo ``procedencia`` de cada dato para que el
frontend pueda distinguir real / simulado / pendiente.
"""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from app.db.economy import TransactionError, depositar_dinero, transfer_money, transferir_pokemon
from app.db.models import Base, Trainer, TrainerWallet, TrainerWorldState
from app.db.session import make_engine, make_session_factory
from app.emulator.adapter import EmulatorAdapter
from app.simulation.engine import AgenteSim, ConfigSimulacion, SimulationEngine

logger = logging.getLogger("pbm.api")


class State:
    """Contenedor de recursos compartidos por la app (inyectable en tests)."""

    def __init__(self) -> None:
        self.engine = make_engine()
        Base.metadata.create_all(self.engine)
        self.SessionLocal = make_session_factory(self.engine)
        self.adapter = EmulatorAdapter()
        self.ws_clients: set[WebSocket] = set()
        self.sim_running = False


def create_app(state: State | None = None) -> FastAPI:
    st = state or State()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.pbm = st
        yield

    app = FastAPI(title="Pokémon Bot Manager — API local", version="0.1.0",
                  lifespan=lifespan)

    # ------------------------------------------------------------- utilidades
    def broadcast(payload: dict[str, Any]) -> None:
        for ws in list(st.ws_clients):
            try:
                asyncio.get_running_loop().create_task(ws.send_json(payload))
            except RuntimeError:
                pass

    # ------------------------------------------------------------------- salud
    @app.get("/api/estado")
    def estado() -> dict:
        """Estado del emulador: solo datos REALMENTE observados por el puente."""
        s = st.adapter.state
        return {
            "conectado": s.connected,
            "rom_cargada": s.rom_loaded,
            "mgba_version": s.mgba_version,
            "game_code": s.game_code,
            "frame": s.frame,
            "parada_emergencia": s.emergency_stop,
            "control_manual": s.manual_control_active,
            "ultimo_error": s.last_error,
            "procedencia": "real" if s.connected else "desconectado",
        }

    # ---------------------------------------------------------------- entrenadores
    class TrainerCreate(BaseModel):
        nombre: str = Field(min_length=2, max_length=40)
        especialidad: str = "explorador"
        saldo_inicial: int = Field(default=500, ge=0, le=999999)

    @app.post("/api/entrenadores")
    def crear_entrenador(datos: TrainerCreate) -> dict:
        with st.SessionLocal() as session:
            if session.query(Trainer).filter_by(nombre=datos.nombre).first():
                raise HTTPException(409, "nombre de entrenador duplicado")
            t = Trainer(nombre=datos.nombre, especialidad=datos.especialidad)
            session.add(t)
            session.flush()
            session.add(TrainerWallet(trainer_id=t.id, saldo=datos.saldo_inicial))
            session.add(TrainerWorldState(trainer_id=t.id))
            session.commit()
            return {"id": t.id, "nombre": t.nombre, "especialidad": datos.especialidad}

    @app.get("/api/entrenadores")
    def listar_entrenadores() -> list[dict]:
        with st.SessionLocal() as session:
            out = []
            for t in session.query(Trainer).all():
                w = session.get(TrainerWallet, t.id)
                ws = session.get(TrainerWorldState, t.id)
                out.append({
                    "id": t.id, "nombre": t.nombre, "nivel": t.nivel,
                    "especialidad": t.especialidad.value,
                    "estado_agente": t.estado_agente.value,
                    "modo_control": t.estado_control.value,
                    "saldo": w.saldo if w else 0,
                    "ubicacion": ws.ubicacion_logica if ws else "?",
                    "objetivo": ws.objetivo_actual if ws else None,
                    "procedencia": "simulado",  # gestor lógico, no ROM
                })
            return out

    # ---------------------------------------------------------------- economía
    class Deposito(BaseModel):
        trainer_id: str
        cantidad: int = Field(gt=0)
        idempotency_key: str = Field(min_length=8, max_length=64)
        administrativo: bool = False

    @app.post("/api/almacen/depositar-dinero")
    def depositar(d: Deposito) -> dict:
        with st.SessionLocal() as session:
            try:
                res = depositar_dinero(session, d.trainer_id, d.cantidad,
                                       d.idempotency_key, exigir_centro=not d.administrativo)
            except TransactionError as exc:
                raise HTTPException(422, str(exc))
            broadcast({"canal": "economia", "tipo": "deposito", **res})
            return res

    class Transferencia(BaseModel):
        origen: str
        destino: str
        cantidad: int = Field(gt=0)
        idempotency_key: str = Field(min_length=8, max_length=64)

    @app.post("/api/economia/transferir")
    def transferir(t: Transferencia) -> dict:
        with st.SessionLocal() as session:
            try:
                res = transfer_money(session, origen=t.origen, destino=t.destino,
                                     cantidad=t.cantidad, idem=t.idempotency_key)
            except TransactionError as exc:
                raise HTTPException(422, str(exc))
            broadcast({"canal": "economia", "tipo": "transferencia", **res})
            return res

    # ---------------------------------------------------------------- control
    @app.post("/api/emergencia/parar")
    async def emergencia_parar() -> dict:
        await st.adapter.emergency_stop()
        broadcast({"canal": "control", "tipo": "emergencia", "activa": True})
        return {"parada_emergencia": True,
                "nota": "No se enviarán más entradas automáticas hasta liberar."}

    @app.post("/api/emergencia/liberar")
    async def emergencia_liberar() -> dict:
        try:
            await st.adapter.emergency_release()
        except (ConnectionError, TimeoutError) as exc:
            raise HTTPException(503, f"puente no disponible: {exc}")
        broadcast({"canal": "control", "tipo": "emergencia", "activa": False})
        return {"parada_emergencia": st.adapter.state.emergency_stop}

    class TomaControl(BaseModel):
        trainer_id: str

    @app.post("/api/control/tomar")
    def tomar_control(c: TomaControl) -> dict:
        """El usuario toma control manual: la automatización se detiene ANTES."""
        st.adapter.acquire_manual_control()
        with st.SessionLocal() as session:
            t = session.get(Trainer, c.trainer_id)
            if not t:
                raise HTTPException(404, "entrenador inexistente")
            from app.db.models import EstadoControl
            t.estado_control = EstadoControl.MANUAL
            session.commit()
        return {"manual": True, "trainer_id": c.trainer_id,
                "advertencia": "La partida mostrada sigue siendo la del protagonista actual "
                               "de la ROM; los demás entrenadores son simulados."}

    @app.post("/api/control/devolver")
    async def devolver_control(c: TomaControl) -> dict:
        """Devuelve el control al sistema: exige releer estado antes de reanudar."""
        st.adapter.release_manual_control()
        try:
            fresh = await st.adapter.ping()
        except (ConnectionError, TimeoutError):
            fresh = False
        with st.SessionLocal() as session:
            from app.db.models import EstadoControl
            t = session.get(Trainer, c.trainer_id)
            if not t:
                raise HTTPException(404, "entrenador inexistente")
            t.estado_control = EstadoControl.AUTOMATICO
            session.commit()
        return {"manual": False, "estado_releido": fresh,
                "nota": None if fresh else "sin verificación del emulador: agentes en pausa"}

    # ---------------------------------------------------------------- memoria
    class LecturaMemoria(BaseModel):
        region: str
        address: int = Field(ge=0)
        width: int = Field(default=1, in_=[1, 2, 4])

    @app.post("/api/memoria/leer")
    async def leer_memoria(l: LecturaMemoria) -> dict:
        """Lectura verificada vía puente. value=null => NO se pudo leer (nunca se inventa)."""
        try:
            value = await st.adapter.read_memory(l.region, l.address, l.width)
        except ConnectionError:
            raise HTTPException(503, "puente mGBA no conectado")
        return {"region": l.region, "address": l.address, "width": l.width,
                "value": value, "procedencia": "real" if value is not None else "no_leido"}

    # ---------------------------------------------------------------- websocket
    @app.websocket("/ws/eventos")
    async def ws_eventos(ws: WebSocket):
        await ws.accept()
        st.ws_clients.add(ws)
        try:
            while True:
                data = await ws.receive_text()
                if data == "ping":
                    await ws.send_json({"canal": "hs", "tipo": "pong"})
        except WebSocketDisconnect:
            pass
        finally:
            st.ws_clients.discard(ws)

    return app


app = create_app()
