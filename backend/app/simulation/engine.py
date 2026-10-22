"""Motor de simulación determinista (Fase 3).

Garantías:
- Mismo estado inicial + misma configuración + misma versión de reglas
  ⇒ mismo resultado (verificado por hash del estado final).
- Aleatoriedad controlada: PRNG propio con semilla explícita y guardada.
- Pasos discretos (``tick``); sin tiempo wall-clock.
- Versión de reglas identificable en cada ejecución (``SimulationRun``).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

MOTOR_VERSION = "sim-0.1.0"
REGLAS_VERSION = "v1"


class DeterministicRandom:
    """LCG simple y portable (no depende de la plataforma ni de time())."""

    def __init__(self, seed: int) -> None:
        self.state = seed & 0xFFFFFFFFFFFFFFFF

    def next_u64(self) -> int:
        # Constants de SplitMix64 — deterministas en cualquier Python.
        self.state = (self.state + 0x9E3779B97F4A7C15) & 0xFFFFFFFFFFFFFFFF
        z = self.state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & 0xFFFFFFFFFFFFFFFF
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & 0xFFFFFFFFFFFFFFFF
        return z ^ (z >> 31)

    def randint(self, a: int, b: int) -> int:
        if a > b:
            raise ValueError("rango inválido")
        return a + self.next_u64() % (b - a + 1)

    def chance(self, numerator: int, denominator: int) -> bool:
        return self.randint(1, denominator) <= numerator


# ------------------------------------------------------------------ especialidades
@dataclass(frozen=True)
class EfectoEspecialidad:
    """Efectos numéricos documentados y configurables (requisito 2.9)."""

    explorador: float = 1.20       # velocidad de desplazamiento x1.2
    combatiente: float = 1.25      # eficiencia en combate simulado
    capturador: float = 1.30       # eficiencia de captura (num/den sobre tabla base)
    recolector: float = 1.15       # rendimiento de recursos
    superviviente_recuperacion: float = 0.70  # reduce tiempo de recuperación al 70%


DEFAULT_ESPECIALIDADES = EfectoEspecialidad()


class EstadoAgente(str, Enum):
    IDLE = "idle"
    PREPARING = "preparing"
    WORKING = "working"
    RETURNING = "returning"
    RECOVERING = "recovering"
    BLOCKED = "blocked"
    STOPPED = "stopped"


TRANSICIONES_VALIDAS: dict[EstadoAgente, set[EstadoAgente]] = {
    EstadoAgente.IDLE: {EstadoAgente.PREPARING, EstadoAgente.STOPPED},
    EstadoAgente.PREPARING: {EstadoAgente.WORKING, EstadoAgente.BLOCKED,
                             EstadoAgente.RETURNING, EstadoAgente.STOPPED},
    EstadoAgente.WORKING: {EstadoAgente.RETURNING, EstadoAgente.RECOVERING,
                           EstadoAgente.BLOCKED, EstadoAgente.IDLE, EstadoAgente.STOPPED},
    EstadoAgente.RETURNING: {EstadoAgente.IDLE, EstadoAgente.RECOVERING,
                             EstadoAgente.BLOCKED, EstadoAgente.STOPPED},
    EstadoAgente.RECOVERING: {EstadoAgente.IDLE, EstadoAgente.BLOCKED,
                              EstadoAgente.STOPPED},
    EstadoAgente.BLOCKED: {EstadoAgente.IDLE, EstadoAgente.PREPARING,
                           EstadoAgente.RECOVERING, EstadoAgente.STOPPED},
    EstadoAgente.STOPPED: {EstadoAgente.IDLE},
}


class TransicionInvalida(RuntimeError):
    pass


@dataclass
class AgenteSim:
    """Estado lógico de un entrenador dentro del simulador."""

    trainer_id: str
    nombre: str
    especialidad: str
    nivel: int = 5
    dinero: int = 500
    pokeballs: int = 5
    salud: int = 100          # 0..100 (porcentaje del equipo)
    ubicacion: str = "Pueblo Paleta"
    objetivo: str | None = None
    estado: EstadoAgente = EstadoAgente.IDLE
    motivo_estado: str = "creado"
    recuperacion_restante: int = 0
    ticks_sin_progreso: int = 0
    historial: list[dict] = field(default_factory=list)

    def transicionar(self, nuevo: EstadoAgente, motivo: str, tick: int) -> None:
        if nuevo == self.estado:
            return
        if nuevo not in TRANSICIONES_VALIDAS[self.estado]:
            raise TransicionInvalida(
                f"{self.estado.value} -> {nuevo.value} no permitida ({motivo})")
        self.estado = nuevo
        self.motivo_estado = motivo
        self.historial.append({"tick": tick, "estado": nuevo.value, "motivo": motivo})


@dataclass
class ConfigSimulacion:
    semilla: int = 1234
    pasos_max_por_tarea: int = 200
    prob_exito_combate_base_num: int = 7     # 7/10 modificada por especialidad/nivel
    prob_exito_combate_base_den: int = 10
    prob_captura_base_num: int = 4
    prob_captura_base_den: int = 10
    costo_curacion: int = 0                  # en RF curar es gratis; penalizaciones son aparte
    penalizacion_derrota: int = 50           # economía SIMULADA, nunca aplicada a la ROM real
    ticks_recuperacion: int = 10
    umbral_salud_regression: int = 30        # política: volver al Centro bajo este umbral
    max_reintentos_sin_progreso: int = 5


def _hash_estado(agentes: list[AgenteSim]) -> str:
    datos = [asdict(a) for a in agentes]
    blob = json.dumps(datos, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class SimulationEngine:
    """Motor de pasos discretos. No toca el emulador: todo resultado es 'simulado'."""

    def __init__(self, config: ConfigSimulacion, agentes: list[AgenteSim]) -> None:
        self.config = config
        self.agentes = {a.trainer_id: a for a in agentes}
        self.rng = DeterministicRandom(config.semilla)
        self.tick_actual = 0
        self.eventos: list[dict] = []   # AgentEvent lógicos (procedencia=simulado)
        self.ejecucion_hash_inicial = _hash_estado(list(self.agentes.values()))
        self.efectos = DEFAULT_ESPECIALIDADES

    # ------------------------------------------------------------- utilidades
    def _emitir(self, tipo: str, trainer_id: str, **datos: Any) -> None:
        dedupe = f"{self.tick_actual}:{trainer_id}:{tipo}:{json.dumps(datos, sort_keys=True)}"
        self.eventos.append({"dedupe_key": dedupe, "tick": self.tick_actual,
                             "trainer_id": trainer_id, "tipo": tipo, "datos": datos,
                             "procedencia": "simulado"})

    def _eficiencia(self, agente: AgenteSim, clave: str) -> float:
        if agente.especialidad == clave:
            return getattr(self.efectos, clave)
        return 1.0

    # ---------------------------------------------------------------- políticas
    # (Las reglas de salud/recursos se aplican directamente en _step_agente y
    #  _trabajo_caza para mantener un único punto de decisión trazable.)

    # ------------------------------------------------------------------ pasos
    def step(self) -> None:
        self.tick_actual += 1
        for agente in self.agentes.values():
            self._step_agente(agente)

    def _step_agente(self, a: AgenteSim) -> None:
        if a.estado == EstadoAgente.STOPPED:
            return
        if a.estado == EstadoAgente.RECOVERING:
            a.recuperacion_restante -= 1
            if a.recuperacion_restante <= 0:
                a.salud = 100
                a.transicionar(EstadoAgente.IDLE, "recuperacion_completa", self.tick_actual)
                self._emitir("recuperacion_completada", a.trainer_id)
            return
        if a.estado == EstadoAgente.BLOCKED:
            return
        if a.estado == EstadoAgente.IDLE:
            if a.objetivo:
                a.transicionar(EstadoAgente.PREPARING, "objetivo_asignado", self.tick_actual)
            return
        if a.estado == EstadoAgente.PREPARING:
            if a.objetivo == "cazar_ratas" and a.pokeballs <= 0:
                a.transicionar(EstadoAgente.BLOCKED, "sin_pokeballs", self.tick_actual)
                self._emitir("bloqueo", a.trainer_id, motivo="sin_pokeballs")
                return
            a.transicionar(EstadoAgente.WORKING, "preparacion_ok", self.tick_actual)
            return
        if a.estado == EstadoAgente.WORKING:
            if a.objetivo == "cazar_ratas":
                self._trabajo_caza(a)
            elif a.objetivo == "explorar_ruta":
                self._trabajo_explorar(a)
            else:
                a.transicionar(EstadoAgente.RETURNING, "objetivo_desconocido", self.tick_actual)
            return
        if a.estado == EstadoAgente.RETURNING:
            if a.ubicacion != "Pueblo Paleta":
                vel = self._eficiencia(a, "explorador")
                a.ubicacion = "Pueblo Paleta"
                self._emitir("regreso", a.trainer_id, velocidad=vel)
            a.salud = min(100, a.salud + 40)  # curación lógica en el Centro
            a.ubicacion = "Centro Pokémon"
            a.transicionar(EstadoAgente.IDLE, "entrega_completada", self.tick_actual)
            self._emitir("curacion_logica", a.trainer_id, salud=a.salud)

    def _trabajo_caza(self, a: AgenteSim) -> None:
        num = self.config.prob_exito_combate_base_num
        den = self.config.prob_exito_combate_base_den
        bonus = self._eficiencia(a, "combatiente") * (1 + a.nivel / 100.0)
        exito = self.rng.chance(int(num * bonus), den)
        if exito:
            a.dinero += 45  # botín simulado
            a.ticks_sin_progreso = 0
            self._emitir("victoria_simulada", a.trainer_id, botin=45)
            cap_num = int(self.config.prob_captura_base_num * self._eficiencia(a, "capturador"))
            if a.pokeballs > 0 and self.rng.chance(cap_num, self.config.prob_captura_base_den):
                a.pokeballs -= 1
                self._emitir("captura_simulada", a.trainer_id, especie="Rattata")
            if self.rng.chance(1, 10):
                a.objetivo = None
                a.transicionar(EstadoAgente.RETURNING, "tarea_completada", self.tick_actual)
        else:
            daño = 25
            a.salud = max(0, a.salud - daño)
            a.ticks_sin_progreso += 1
            self._emitir("derrota_parcial", a.trainer_id, dano=daño)
            if a.salud < self.config.umbral_salud_regression:
                self._aplicar_derrota(a)
            elif a.ticks_sin_progreso >= self.config.max_reintentos_sin_progreso:
                a.transicionar(EstadoAgente.RETURNING, "sin_progreso_repetido",
                               self.tick_actual)

    def _trabajo_explorar(self, a: AgenteSim) -> None:
        vel = self._eficiencia(a, "explorador")
        avance = int(10 * vel)
        ctx = {"avance": avance}
        a.ticks_sin_progreso = 0
        self._emitir("exploracion", a.trainer_id, avance=avance)
        if self.rng.chance(1, 8):
            a.objetivo = None
            a.transicionar(EstadoAgente.RETURNING, "ruta_descubierta", self.tick_actual)

    def _aplicar_derrota(self, a: AgenteSim) -> None:
        """Derrota: penalización SOLO en economía simulada + recuperación determinista."""
        penal = self.config.penalizacion_derrota
        a.dinero = max(0, a.dinero - penal)
        rec = int(self.config.ticks_recuperacion)
        if a.especialidad == "superviviente":
            rec = int(rec * self.efectos.superviviente_recuperacion)
        a.recuperacion_restante = rec
        a.ubicacion = "Centro Pokémon"
        a.transicionar(EstadoAgente.RECOVERING, "derrota", self.tick_actual)
        self._emitir("derrota", a.trainer_id, penalizacion_simulada=penal,
                     ticks_recuperacion=rec)

    # ------------------------------------------------------------------- run
    def run(self, ticks: int) -> dict:
        for _ in range(ticks):
            self.step()
        resultado_hash = _hash_estado(list(self.agentes.values()))
        return {
            "motor_version": MOTOR_VERSION,
            "reglas_version": REGLAS_VERSION,
            "semilla": self.config.semilla,
            "instante_inicio": 0,
            "instante_fin": self.tick_actual,
            "config_hash": hashlib.sha256(
                json.dumps(asdict(self.config), sort_keys=True).encode()).hexdigest(),
            "resultado_hash": resultado_hash,
            "eventos": len(self.eventos),
        }
