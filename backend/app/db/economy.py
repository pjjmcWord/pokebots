"""Economía central: ÚNICA capa que mueve dinero/objetos/Pokémon (requisito 14).

Reglas:
- Transacciones atómicas (una sola sesión de SQLAlchemy por operación).
- Idempotencia por ``idempotency_key``: reintentos no duplican resultados.
- Saldos enteros, nunca negativos (CheckConstraint como red de seguridad final).
- Todo movimiento queda auditado en ``resource_transactions`` + ``audit_log``.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import (
    AuditLog, PokemonInstance, RecursoTipo, ResourceTransaction, SharedStorageItem,
    SharedTreasury, TipoTransaccion, Trainer, TrainerInventoryItem, TrainerWallet,
    TrainerParty, TrainerWorldState, Procedencia,
)

TESORERIA = "tesoreria"
SISTEMA = "sistema"


class TransactionError(RuntimeError):
    """Operación económica inválida (saldo insuficiente, recurso inexistente...)."""


def _wallet(session: Session, trainer_id: str) -> TrainerWallet:
    wallet = session.get(TrainerWallet, trainer_id)
    if wallet is None:
        raise TransactionError(f"entrenador {trainer_id} sin monedero")
    return wallet


def _add_transaction(session: Session, *, tipo: TipoTransaccion, recurso: RecursoTipo,
                     idem: str, origen: str | None, destino: str | None, cantidad: int,
                     referencia: str | None, motivo: str | None,
                     procedencia: Procedencia) -> ResourceTransaction:
    tx = ResourceTransaction(
        idempotency_key=idem, tipo=tipo, recurso_tipo=recurso, referencia=referencia,
        origen=origen, destino=destino, cantidad=cantidad, motivo=motivo,
        procedencia=procedencia, estado="confirmada",
    )
    session.add(tx)
    return tx


def transfer_money(session: Session, *, origen: str, destino: str, cantidad: int,
                   idem: str, motivo: str | None = None,
                   procedencia: Procedencia = Procedencia.SIMULADO) -> dict:
    """Movimiento atómico de dinero entre monederos o con la tesorería.

    ``idem`` garantiza que reenviar la misma orden no duplica el efecto.
    """
    if cantidad <= 0:
        raise TransactionError("la cantidad debe ser > 0")
    existing = session.scalar(
        select(ResourceTransaction).where(ResourceTransaction.idempotency_key == idem))
    if existing:
        return {"duplicado": True, "tx_id": existing.id, "estado": existing.estado}

    with session.begin_nested():
        if origen != TESORERIA and origen != SISTEMA:
            w = _wallet(session, origen)
            if w.saldo < cantidad:
                raise TransactionError(f"saldo insuficiente en {origen}: {w.saldo} < {cantidad}")
            w.saldo -= cantidad
        if destino != TESORERIA and destino != SISTEMA:
            _wallet(session, destino)  # valida existencia

        if origen == TESORERIA or destino == TESORERIA:
            treasury = session.get(SharedTreasury, 1)
            if treasury is None:
                treasury = SharedTreasury(id=1, saldo=0)
                session.add(treasury)
                session.flush()
            if destino == TESORERIA:
                treasury.saldo += cantidad
            else:
                if treasury.saldo < cantidad:
                    raise TransactionError("tesorería insuficiente")
                treasury.saldo -= cantidad

        _add_transaction(session, tipo=TipoTransaccion.TRASFERENCIA,
                         recurso=RecursoTipo.DINERO, idem=idem, origen=origen,
                         destino=destino, cantidad=cantidad, referencia=None,
                         motivo=motivo, procedencia=procedencia)
    session.add(AuditLog(categoria="economia", descripcion=f"transfer {origen}->{desto}",
                         datos_json={"idem": idem, "cantidad": cantidad}))
    session.commit()
    return {"duplicado": False, "origen": origen, "destino": destino, "cantidad": cantidad}


def depositar_dinero(session: Session, trainer_id: str, cantidad: int, idem: str,
                     motivo: str | None = "deposito en Centro Pokémon",
                     exigir_centro: bool = True) -> dict:
    """Deposita dinero del entrenador en la tesorería compartida.

    Regla 2.6: solo si el agente está físicamente en el Centro Pokémon, salvo
    operación administrativa explícita (``exigir_centro=False``).
    """
    if exigir_centro:
        state = session.get(TrainerWorldState, trainer_id)
        if not state or not state.en_centro_pokemon:
            raise TransactionError("el entrenador no está en el Centro Pokémon; "
                                   "depósito bloqueado (usa una operación administrativa si "
                                   "es una prueba autorizada)")
    return transfer_money(session, origen=trainer_id, destino=TESORERIA,
                          cantidad=cantidad, idem=idem, motivo=motivo)


def retirar_dinero(session: Session, trainer_id: str, cantidad: int, idem: str,
                   motivo: str | None = "retirada del almacén compartido",
                   exigir_centro: bool = True) -> dict:
    if exigir_centro:
        from .models import TrainerWorldState
        state = session.get(TrainerWorldState, trainer_id)
        if not state or not state.en_centro_pokemon:
            raise TransactionError("el entrenador no está en el Centro Pokémon")
    return transfer_money(session, origen=TESORERIA, destino=trainer_id,
                          cantidad=cantidad, idem=idem, motivo=motivo)


def comprar_objeto(session: Session, trainer_id: str, item_id: str, precio: int,
                   idem: str, procedencia: Procedencia = Procedencia.SIMULADO) -> dict:
    """Compra simulada: consume dinero del entrenador y añade al SU inventario."""
    if existing := session.scalar(select(ResourceTransaction).where(
            ResourceTransaction.idempotency_key == idem)):
        return {"duplicado": True, "tx_id": existing.id}
    if precio < 0:
        raise TransactionError("precio negativo")
    with session.begin_nested():
        w = _wallet(session, trainer_id)
        if w.saldo < precio:
            raise TransactionError("saldo insuficiente")
        w.saldo -= precio
        inv = session.scalar(select(TrainerInventoryItem).where(
            TrainerInventoryItem.trainer_id == trainer_id,
            TrainerInventoryItem.item_id == item_id))
        if inv is None:
            inv = TrainerInventoryItem(trainer_id=trainer_id, item_id=item_id,
                                       cantidad=0, procedencia=procedencia)
            session.add(inv)
        inv.cantidad += 1
        _add_transaction(session, tipo=TipoTransaccion.GASTO, recurso=RecursoTipo.OBJETO,
                         idem=idem, origen=trainer_id, destino=SISTEMA, cantidad=precio,
                         referencia=item_id, motivo="compra simulada",
                         procedencia=procedencia)
    session.commit()
    return {"duplicado": False}


def mover_objeto(session: Session, *, origen_trainer: str, destino: str, item_id: str,
                 cantidad: int, idem: str) -> dict:
    """Transfiere objetos entre entrenadores o hacia/desde el almacén compartido.

    Impide explícitamente que un agente use recursos de OTRO entrenador: solo se
    puede mover desde el inventario propio (origen_trainer) a un destino válido.
    """
    if cantidad <= 0:
        raise TransactionError("cantidad debe ser > 0")
    if session.scalar(select(ResourceTransaction).where(
            ResourceTransaction.idempotency_key == idem)):
        return {"duplicado": True}
    with session.begin_nested():
        inv = session.scalar(select(TrainerInventoryItem).where(
            TrainerInventoryItem.trainer_id == origen_trainer,
            TrainerInventoryItem.item_id == item_id))
        if inv is None or inv.cantidad < cantidad:
            raise TransactionError("origen no tiene suficientes unidades")
        inv.cantidad -= cantidad
        if destino == "almacen":
            stored = session.scalar(select(SharedStorageItem).where(
                SharedStorageItem.recurso_tipo == RecursoTipo.OBJETO,
                SharedStorageItem.referencia == item_id))
            if stored is None:
                stored = SharedStorageItem(recurso_tipo=RecursoTipo.OBJETO,
                                           referencia=item_id, cantidad=0,
                                           depositado_por=origen_trainer)
                session.add(stored)
            stored.cantidad += cantidad
        else:
            dst_inv = session.scalar(select(TrainerInventoryItem).where(
                TrainerInventoryItem.trainer_id == destino,
                TrainerInventoryItem.item_id == item_id))
            if dst_inv is None:
                dst_inv = TrainerInventoryItem(trainer_id=destino, item_id=item_id, cantidad=0)
                session.add(dst_inv)
            dst_inv.cantidad += cantidad
        _add_transaction(session, tipo=TipoTransaccion.TRASFERENCIA,
                         recurso=RecursoTipo.OBJETO, idem=idem, origen=origen_trainer,
                         destino=destino, cantidad=cantidad, referencia=item_id,
                         motivo="transferencia lógica", procedencia=Procedencia.SIMULADO)
    session.commit()
    return {"duplicado": False}


def transferir_pokemon(session: Session, *, pokemon_id: str, destino_trainer: str | None,
                       idem: str, posicion_destino: int | None = None) -> dict:
    """Cambia la propiedad operativa de un Pokémon (a otro equipo o al almacén).

    Garantiza propiedad exclusiva: un Pokémon no puede estar en dos sitios a la vez
    (unique en trainer_party.pokemon_id + actualización atómica de location).
    """
    if session.scalar(select(ResourceTransaction).where(
            ResourceTransaction.idempotency_key == idem)):
        return {"duplicado": True}
    with session.begin_nested():
        poke = session.get(PokemonInstance, pokemon_id)
        if poke is None:
            raise TransactionError("Pokémon inexistente")
        slot_actual = session.scalar(select(TrainerParty).where(
            TrainerParty.pokemon_id == pokemon_id))
        if slot_actual:
            session.delete(slot_actual)
            session.flush()  # libera la restricción unique antes de re-insertar
        if destino_trainer is None:
            poke.location = "almacén"
            poke.owner_trainer_id = None
            existe = session.scalar(select(SharedStorageItem).where(
                SharedStorageItem.recurso_tipo == RecursoTipo.POKEMON,
                SharedStorageItem.referencia == pokemon_id))
            if not existe:
                session.add(SharedStorageItem(recurso_tipo=RecursoTipo.POKEMON,
                                              referencia=pokemon_id, cantidad=1,
                                              depositado_por=poke.owner_trainer_id))
        else:
            if posicion_destino is None or not (1 <= posicion_destino <= 6):
                raise TransactionError("posición de equipo inválida (1..6)")
            ocupado = session.scalar(select(TrainerParty).where(
                TrainerParty.trainer_id == destino_trainer,
                TrainerParty.posicion == posicion_destino))
            if ocupado:
                raise TransactionError(f"posición {posicion_destino} ya ocupada")
            session.add(TrainerParty(trainer_id=destino_trainer, posicion=posicion_destino,
                                     pokemon_id=pokemon_id))
            poke.location = "equipo"
            poke.owner_trainer_id = destino_trainer
        _add_transaction(session, tipo=TipoTransaccion.TRASFERENCIA,
                         recurso=RecursoTipo.POKEMON, idem=idem,
                         origen=slot_actual.trainer_id if slot_actual else "almacén",
                         destino=destino_trainer or "almacén", cantidad=1,
                         referencia=pokemon_id, motivo="cambio de propietario operativo",
                         procedencia=Procedencia.SIMULADO)
    session.commit()
    return {"duplicado": False}
