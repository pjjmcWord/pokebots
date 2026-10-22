# Pokémon Bot Manager

Juego de gestión incremental en el que varios entrenadores progresan en un mundo basado en **Pokémon Rojo Fuego**, utilizando una emulación real del juego (mGBA) como autoridad del "mundo real" y una capa de simulación determinista para los agentes secundarios.

> ⚠️ **Aviso legal:** este proyecto **no incluye ni distribuye** ROMs, BIOS ni ningún recurso protegido de Nintendo/Game Freak. El usuario debe aportar legalmente su propia copia de *Pokémon Rojo Fuego* (revisión USA `BPRE`, se recomienda verificar el hash SHA-1). Existe un **modo de demostración sin ROM**.

## Estado actual del proyecto

| Fase | Descripción | Estado |
|------|-------------|--------|
| 0 | Preparación del repositorio y documentación | ✅ Completada |
| 1 | Prueba de viabilidad del emulador (protocolo Lua ↔ Python) | 🟡 Implementada y probada con simulador; pendiente de validación visual con mGBA + ROM en máquina con GUI |
| 2 | Backend y persistencia (SQLite + migraciones) | ✅ Esqueleto implementado (modelo, transacciones, API) |
| 3 | Simulador determinista de entrenadores | ✅ Motor inicial con pruebas de reproducibilidad |
| 4 | Interfaz web local | 🟡 Inicial (Vite + React, pantalla principal) |
| 5-10 | Almacén compartido, control manual/automático, integración… | 📋 Planificado (ver `docs/roadmap.md`) |

## Estructura del repositorio

```
pokemon-bot-manager/
├── README.md / LICENSE / CONTRIBUTING.md / CHANGELOG.md
├── docs/                 # Arquitectura, roadmap, decisiones (ADR), pruebas, integración
│   └── decisions/        # Registros de decisiones arquitectónicas
├── backend/              # API FastAPI + SQLite + simulador + adaptador de emulador
│   ├── app/
│   │   ├── api/          # Rutas REST y WebSocket
│   │   ├── db/           # Modelos SQLAlchemy y migraciones Alembic
│   │   ├── emulator/     # Cliente TCP del script Lua (adaptador aislado)
│   │   ├── simulation/   # Motor determinista y máquina de estados
│   │   └── schemas/      # Modelos Pydantic
│   └── tests/            # Pruebas pytest (unitarias y de integración simulada)
├── emulator/             # Scripts Lua para mGBA y definición del protocolo
│   ├── lua/
│   └── protocol/
├── frontend/             # Aplicación web local (TypeScript + React + Vite)
├── shared/schemas/       # Esquema JSON del protocolo (única fuente de verdad)
├── scripts/              # Utilidades (arranque, verificación de ROM, CI local)
├── tests/integration/    # Pruebas de integración con emulador real (opcionales)
└── .github/workflows/    # CI (pytest, lint)
```

## Requisitos

- **Python ≥ 3.11** y Node.js ≥ 18 (desarrollo probado con Python 3.12 / Node 20).
- **mGBA ≥ 0.10** con soporte de scripting Lua (versión 0.10.1 verificada en Debian; en Windows usar el instalador oficial de https://mgba.io/downloads.html).
- Una ROM legal de *Pokémon Rojo Fuego* (USA, `BPRE`) — **no incluida**.

## Instalación y ejecución (desarrollo)

### Backend

```bash
cd backend
python -m venv .venv
# Windows: .venv\Scripts\activate   Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"
alembic upgrade head                       # crea la base de datos SQLite
uvicorn app.main:app --reload --port 8000  # API local en http://127.0.0.1:8000/docs
```

### Pruebas

```bash
cd backend
pytest -q          # unitarias + integración simulada (no requieren ROM ni GUI)
pytest -m emulator # pruebas reales de emulador (requieren mGBA + ROM; se omiten si faltan)
```

### Emulador con script de puente (validación manual de la Fase 1)

```bash
# Con mGBA instalado y una ROM legal:
mgba emulator/lua/pbm_bridge.lua <ruta-legal>/fire_red.gba
# Dentro del emulador: Scripting > Scripts > pbm_bridge.lua
# El script abre un servidor TCP en 127.0.0.1:42070.
python scripts/comprobar_puente.py   # verifica handshake, estado y entrada controlada
```

En entornos sin interfaz gráfica (CI, servidores) no es posible cargar la ROM; el
simulador del backend (`backend/app/emulator/mock.py`) valida el mismo protocolo.

### Frontend

```bash
cd frontend
npm install
npm run dev      # http://localhost:5173 (conecta con la API local por WS/REST)
```

## Seguridad

- Todos los servicios escuchan **solo en 127.0.0.1**.
- El protocolo entre el script Lua y el backend es versionado y validado (`shared/schemas/protocol.schema.json`).
- Prioridad absoluta del **control manual**: las automatizaciones se detienen antes de ceder entradas al usuario y hay **parada de emergencia** accesible desde la API y la interfaz.
- Nunca se afirman datos del juego que no se hayan leído realmente del emulador: todo dato lleva marca de procedencia (`real` / `simulado` / `pendiente`).

## Documentación clave

- [`docs/architecture.md`](docs/architecture.md) — arquitectura definitiva de la v1.
- [`docs/emulator-integration.md`](docs/emulator-integration.md) — investigación de mGBA/Lua y resultados de la Fase 1.
- [`docs/decisions/`](docs/decisions/) — ADRs (elección de emulador, canal de comunicación, licencia, etc.).
- [`docs/roadmap.md`](docs/roadmap.md) — fases y criterios de aceptación.
- [`docs/testing.md`](docs/testing.md) — cómo se prueba cada capa.

## Licencia

Código propio bajo **MIT** (ver `LICENSE`). mGBA se usa como programa externo bajo su licencia MPL-2.0; no se modifican ni redistribuyen sus fuentes. Las dependencias y sus licencias están documentadas en [`docs/dependencies.md`](docs/dependencies.md).
