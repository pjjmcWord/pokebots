"""Modelos SQLAlchemy — esquema relacional del gestor (Fase 2).

Convenciones:
- Todos los montones monetarios son ENTEROS (₽ simuladas); nada de floats.
- Columna ``procedencia`` en toda entidad con datos de juego: 'real' | 'simulado'.
  Ningún valor se presenta como confirmado en la ROM si no fue leído del emulador.
- Claves foráneas con ON DELETE explícito y restricciones UNIQUE para evitar
  duplicaciones (idempotencia económica, propiedad exclusiva de Pokémon).
"""
from __future__ import annotations

import datetime as dt
import uuid
from enum import Enum

from sqlalchemy import (
    Boolean, CheckConstraint, DateTime, Enum as SAEnum, Float, ForeignKey, Index,
    Integer, JSON, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _uuid() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


def _ts():
    return dt.datetime.now(dt.timezone.utc)


class Especialidad(str, Enum):
    EXPLORADOR = "explorador"
    COMBATIENTE = "combatiente"
    CAPTURADOR = "capturador"
    RECOLECTOR = "recolector"
    SUPERVIVIENTE = "superviviente"


class EstadoControl(str, Enum):
    MANUAL = "manual"
    AUTOMATICO = "automatico"
    PAUSADO = "pausado"
    BLOQUEADO = "bloqueado"


class EstadoAgente(str, Enum):
    """Máquina de estados de la Fase 6/3 (separada del estado de la tarea)."""

    IDLE = "idle"
    PREPARING = "preparing"
    WORKING = "working"
    RETURNING = "returning"
    RECOVERING = "recovering"
    BLOCKED = "blocked"
    STOPPED = "stopped"


class Procedencia(str, Enum):
    REAL = "real"        # confirmado por lectura del emulador
    SIMULADO = "simulado"  # producido por el motor determinista
    PENDIENTE = "pendiente"  # registrado pero sin verificar


class TipoTransaccion(str, Enum):
    DEPOSITO = "deposito"
    RETIRADA = "retirada"
    TRASFERENCIA = "transferencia"
    INGRESO = "ingreso"          # botín, ventas
    GASTO = "gasto"              # compras, penalizaciones simuladas
    PENALIZACION_DERROTA = "penalizacion_derrota"


class RecursoTipo(str, Enum):
    DINERO = "dinero"
    OBJETO = "objeto"
    POKEMON = "pokemon"


class EstadoTarea(str, Enum):
    PENDIENTE = "pendiente"
    ACTIVA = "activa"
    BLOQUEADA = "bloqueada"
    COMPLETADA = "completada"
    FALLIDA = "fallida"
    CANCELADA = "cancelada"


# ------------------------------------------------------------------ entrenador
class Trainer(Base):
    __tablename__ = "trainers"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    nombre: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    nivel: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    especialidad: Mapped[Especialidad] = mapped_column(
        SAEnum(Especialidad, native_enum=False), default=Especialidad.EXPLORADOR)
    estado_control: Mapped[EstadoControl] = mapped_column(
        SAEnum(EstadoControl, native_enum=False), default=EstadoControl.AUTOMATICO)
    estado_agente: Mapped[EstadoAgente] = mapped_column(
        SAEnum(EstadoAgente, native_enum=False), default=EstadoAgente.IDLE)
    config_json: Mapped[dict] = mapped_column(JSON, default=dict)
    creado_en: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_ts)
    actualizado_en: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_ts, onupdate=_ts)

    wallet: Mapped["TrainerWallet"] = relationship(back_populates="trainer", uselist=False,
                                                   cascade="all, delete-orphan")
    world_state: Mapped["TrainerWorldState"] = relationship(back_populates="trainer",
                                                            uselist=False,
                                                            cascade="all, delete-orphan")
    party_slots: Mapped[list["TrainerParty"]] = relationship(back_populates="trainer",
                                                             cascade="all, delete-orphan")
    inventory: Mapped[list["TrainerInventoryItem"]] = relationship(back_populates="trainer",
                                                                   cascade="all, delete-orphan")


class TrainerWallet(Base):
    __tablename__ = "trainer_wallets"

    trainer_id: Mapped[str] = mapped_column(ForeignKey("trainers.id", ondelete="CASCADE"),
                                            primary_key=True)
    saldo: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    moneda: Mapped[str] = mapped_column(String(8), nullable=False, default="PBM")
    procedencia: Mapped[Procedencia] = mapped_column(
        SAEnum(Procedencia, native_enum=False), default=Procedencia.SIMULADO)
    actualizado_en: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_ts, onupdate=_ts)

    trainer: Mapped[Trainer] = relationship(back_populates="wallet")

    __table_args__ = (CheckConstraint("saldo >= 0", name="ck_saldo_no_negativo"),)


class TrainerInventoryItem(Base):
    __tablename__ = "trainer_inventory"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    trainer_id: Mapped[str] = mapped_column(ForeignKey("trainers.id", ondelete="CASCADE"),
                                            nullable=False)
    item_id: Mapped[str] = mapped_column(String(40), nullable=False)  # p.ej. 'pokeball'
    cantidad: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    procedencia: Mapped[Procedencia] = mapped_column(
        SAEnum(Procedencia, native_enum=False), default=Procedencia.SIMULADO)
    actualizado_en: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_ts, onupdate=_ts)

    trainer: Mapped[Trainer] = relationship(back_populates="inventory")

    __table_args__ = (
        UniqueConstraint("trainer_id", "item_id", name="uq_inventario_item_por_entrenador"),
        CheckConstraint("cantidad >= 0", name="ck_cantidad_no_negativa"),
    )


class PokemonInstance(Base):
    """Instancia ÚNICA de Pokémon; dos Rattata nivel 5 no son intercambiables."""

    __tablename__ = "pokemon_instances"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    especie: Mapped[str] = mapped_column(String(40), nullable=False)
    forma: Mapped[str | None] = mapped_column(String(40))
    nivel: Mapped[int] = mapped_column(Integer, default=1)
    experiencia: Mapped[int] = mapped_column(Integer, default=0)
    ps_actuales: Mapped[int | None] = mapped_column(Integer)
    ps_maximos: Mapped[int | None] = mapped_column(Integer)
    estado_alterado: Mapped[str | None] = mapped_column(String(20))
    naturaleza: Mapped[str | None] = mapped_column(String(20))
    habilidad: Mapped[str | None] = mapped_column(String(40))
    stats_json: Mapped[dict] = mapped_column(JSON, default=dict)
    movimientos_json: Mapped[list] = mapped_column(JSON, default=list)
    procedencia: Mapped[Procedencia] = mapped_column(
        SAEnum(Procedencia, native_enum=False), default=Procedencia.SIMULADO, nullable=False)
    sincronizado_en: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    creado_en: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_ts)

    location: Mapped[str] = mapped_column(
        String(16), default="equipo")  # equipo | almacén | especial
    owner_trainer_id: Mapped[str | None] = mapped_column(
        ForeignKey("trainers.id", ondelete="SET NULL"))

    __table_args__ = (
        CheckConstraint("nivel BETWEEN 1 AND 100", name="ck_nivel_valido"),
        Index("ix_pokemon_especie", "especie"),
    )


class TrainerParty(Base):
    __tablename__ = "trainer_party"

    trainer_id: Mapped[str] = mapped_column(ForeignKey("trainers.id", ondelete="CASCADE"),
                                            primary_key=True)
    posicion: Mapped[int] = mapped_column(Integer, primary_key=True)  # 1..6
    pokemon_id: Mapped[str] = mapped_column(ForeignKey("pokemon_instances.id",
                                                      ondelete="RESTRICT"),
                                            nullable=False)
    incorporado_en: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_ts)

    trainer: Mapped[Trainer] = relationship(back_populates="party_slots")
    pokemon: Mapped[PokemonInstance] = relationship()

    __table_args__ = (
        CheckConstraint("posicion BETWEEN 1 AND 6", name="ck_posicion_equipo"),
        UniqueConstraint("pokemon_id", name="uq_pokemon_un_solo_slot"),  # propiedad exclusiva
    )


class SharedStorageItem(Base):
    """Almacén lógico compartido del Centro Pokémon (NO es el PC de la ROM)."""

    __tablename__ = "shared_storage"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    recurso_tipo: Mapped[RecursoTipo] = mapped_column(
        SAEnum(RecursoTipo, native_enum=False), nullable=False)
    referencia: Mapped[str] = mapped_column(String(40), nullable=False)  # item_id o pokemon_id
    cantidad: Mapped[int] = mapped_column(Integer, default=1)
    depositado_por: Mapped[str | None] = mapped_column(
        ForeignKey("trainers.id", ondelete="SET NULL"))
    procedencia: Mapped[Procedencia] = mapped_column(
        SAEnum(Procedencia, native_enum=False), default=Procedencia.SIMULADO)
    sincronizado_real: Mapped[bool] = mapped_column(Boolean, default=False)
    depositado_en: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_ts)

    __table_args__ = (
        UniqueConstraint("recurso_tipo", "referencia", name="uq_almacen_recurso_unico"),
        CheckConstraint("recurso_tipo != 'pokemon' OR cantidad = 1",
                        name="ck_pokemon_no_apilable"),
    )


class SharedTreasury(Base):
    __tablename__ = "shared_treasury"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    saldo: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    actualizado_en: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_ts, onupdate=_ts)

    __table_args__ = (
        CheckConstraint("saldo >= 0", name="ck_tesoreria_no_negativa"),
        CheckConstraint("id = 1", name="ck_tesoreria_fila_unica"),
    )


class TrainerWorldState(Base):
    __tablename__ = "trainer_world_states"

    trainer_id: Mapped[str] = mapped_column(ForeignKey("trainers.id", ondelete="CASCADE"),
                                            primary_key=True)
    ubicacion_logica: Mapped[str] = mapped_column(String(60), default="Pueblo Paleta")
    progreso: Mapped[str] = mapped_column(String(60), default="inicio")
    objetivo_actual: Mapped[str | None] = mapped_column(String(120))
    contexto_json: Mapped[dict] = mapped_column(JSON, default=dict)
    en_centro_pokemon: Mapped[bool] = mapped_column(Boolean, default=False)
    ultima_sincronizacion: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    recuperacion_hasta: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    trainer: Mapped[Trainer] = relationship(back_populates="world_state")


class GameSession(Base):
    """Sesión del emulador. Se identifica la ROM por hash; NUNCA se guarda su contenido."""

    __tablename__ = "game_sessions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    mgba_version: Mapped[str | None] = mapped_column(String(20))
    game_code: Mapped[str | None] = mapped_column(String(8))
    rom_sha1: Mapped[str | None] = mapped_column(String(40))
    estado_conexion: Mapped[str] = mapped_column(String(16), default="desconectada")
    diagnostico_json: Mapped[dict] = mapped_column(JSON, default=dict)
    iniciada_en: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_ts)
    terminada_en: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class ControlSession(Base):
    __tablename__ = "control_sessions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    trainer_id: Mapped[str] = mapped_column(ForeignKey("trainers.id", ondelete="CASCADE"))
    modo: Mapped[EstadoControl] = mapped_column(SAEnum(EstadoControl, native_enum=False))
    inicio_transicion: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_ts)
    fin_transicion: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    motivo: Mapped[str | None] = mapped_column(Text)


class AgentTask(Base):
    __tablename__ = "agent_tasks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    trainer_id: Mapped[str] = mapped_column(ForeignKey("trainers.id", ondelete="CASCADE"))
    tipo: Mapped[str] = mapped_column(String(40), nullable=False)
    objetivo: Mapped[str | None] = mapped_column(String(120))
    estado: Mapped[EstadoTarea] = mapped_column(SAEnum(EstadoTarea, native_enum=False),
                                                default=EstadoTarea.PENDIENTE)
    prioridad: Mapped[int] = mapped_column(Integer, default=5)
    progreso: Mapped[float] = mapped_column(Float, default=0.0)
    recursos_previstos_json: Mapped[dict] = mapped_column(JSON, default=dict)
    resultado_json: Mapped[dict] = mapped_column(JSON, default=dict)
    reglas_version: Mapped[str] = mapped_column(String(20), default="v1")
    creada_en: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_ts)
    finalizada_en: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_tareas_entrenador_estado", "trainer_id", "estado"),)


class AgentPolicy(Base):
    """Regla automática configurable con versión identificable."""

    __tablename__ = "agent_policies"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    trainer_id: Mapped[str | None] = mapped_column(
        ForeignKey("trainers.id", ondelete="CASCADE"))  # None = global
    nombre: Mapped[str] = mapped_column(String(60), nullable=False)
    condiciones_json: Mapped[dict] = mapped_column(JSON, default=dict)
    acciones_json: Mapped[dict] = mapped_column(JSON, default=dict)
    prioridad: Mapped[int] = mapped_column(Integer, default=5)
    activa: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[str] = mapped_column(String(20), default="v1")


class ResourceTransaction(Base):
    """Transacción económica atómica e idempotente."""

    __tablename__ = "resource_transactions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    tipo: Mapped[TipoTransaccion] = mapped_column(SAEnum(TipoTransaccion, native_enum=False))
    recurso_tipo: Mapped[RecursoTipo] = mapped_column(SAEnum(RecursoTipo, native_enum=False))
    referencia: Mapped[str | None] = mapped_column(String(40))  # item_id/pokemon_id si aplica
    origen: Mapped[str | None] = mapped_column(String(40))   # trainer_id | 'tesoreria' | 'sistema'
    destino: Mapped[str | None] = mapped_column(String(40))
    cantidad: Mapped[int] = mapped_column(Integer, nullable=False)
    motivo: Mapped[str | None] = mapped_column(Text)
    procedencia: Mapped[Procedencia] = mapped_column(
        SAEnum(Procedencia, native_enum=False), default=Procedencia.SIMULADO, nullable=False)
    estado: Mapped[str] = mapped_column(String(16), default="confirmada")  # confirmada|revertida
    creada_en: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_ts)

    __table_args__ = (
        CheckConstraint("cantidad > 0", name="ck_cantidad_positiva"),
        Index("ix_transacciones_tiempo", "creada_en"),
    )


class GameEvent(Base):
    """Evento CONFIRMADO por el emulador. Separado estrictamente de AgentEvent."""

    __tablename__ = "game_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str | None] = mapped_column(ForeignKey("game_sessions.id",
                                                              ondelete="SET NULL"))
    tipo: Mapped[str] = mapped_column(String(40), nullable=False)
    datos_json: Mapped[dict] = mapped_column(JSON, default=dict)
    dedupe_key: Mapped[str | None] = mapped_column(String(64), unique=True)
    procesado: Mapped[bool] = mapped_column(Boolean, default=False)
    ocurrido_en: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_ts)


class AgentEvent(Base):
    """Evento SIMULADO de un agente. Nunca se mezcla con GameEvent."""

    __tablename__ = "agent_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trainer_id: Mapped[str] = mapped_column(ForeignKey("trainers.id", ondelete="CASCADE"))
    tipo: Mapped[str] = mapped_column(String(40), nullable=False)
    datos_json: Mapped[dict] = mapped_column(JSON, default=dict)
    dedupe_key: Mapped[str | None] = mapped_column(String(64), unique=True)
    ocurrido_en: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_ts)

    __table_args__ = (Index("ix_agent_events_entrenador", "trainer_id", "ocurrido_en"),)


class SimulationRun(Base):
    __tablename__ = "simulation_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    motor_version: Mapped[str] = mapped_column(String(20), nullable=False)
    reglas_version: Mapped[str] = mapped_column(String(20), nullable=False)
    semilla: Mapped[int] = mapped_column(Integer, nullable=False)
    instante_inicio: Mapped[int] = mapped_column(Integer, nullable=False)
    instante_fin: Mapped[int] = mapped_column(Integer, nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    resultado_hash: Mapped[str | None] = mapped_column(String(64))
    ejecutada_en: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_ts)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    categoria: Mapped[str] = mapped_column(String(30), nullable=False)
    descripcion: Mapped[str] = mapped_column(Text, nullable=False)
    datos_json: Mapped[dict] = mapped_column(JSON, default=dict)
    creado_en: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_ts)
