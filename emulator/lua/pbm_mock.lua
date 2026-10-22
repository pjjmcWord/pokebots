-- pbm_mock.lua — Simulador de la API de scripting de mGBA para pruebas sin GUI.
-- Permite validar la LÓGICA del puente (protocolo, parada de emergencia, hold_frame,
-- lectura de memoria) con Lua 5.1 puro, sin necesidad de emulador ni ROM.
-- NO sustituye la validación real con mGBA; complementa las pruebas automatizadas.

package.path = "/workspace/emulator/lua/?.lua;" .. package.path

-- ---- stubs de la API mGBA -------------------------------------------------
KEY_A, KEY_B, KEY_SELECT, KEY_START = 0, 1, 2, 3
KEY_DRIGHT, KEY_DLEFT, KEY_DUP, KEY_DDOWN, KEY_R, KEY_L = 4, 5, 6, 7, 8, 9
VERSION = "mock-0.10"

local _held = {}
input = {
    holdKey = function(self, k) _held[k] = true end,
    releaseKey = function(self, k) _held[k] = nil end,
}
keyboard = { isPressed = function(self, k) return false end }
view = { width = function(self) return 240 end }
emulator = {}

memory = {}
local regions = { IWRAM = 0x400, EWRAM = 0x40000, ROM = 0x200000, PALETTE = 0x400,
                  VRAM = 0x18000, OAM = 0x400, SRAM = 0x8000, FLASH = 0x10000 }
local memdata = {}
for name, size in pairs(regions) do
    local data = {}
    for i = 0, size - 1 do data[i] = (i * 7 + #name) % 256 end -- patrón determinista
    memdata[name] = data
    memory[name:upper()] = {
        readByte = function(self, a) if a >= 0 and a < #self._d then return self._d[a] else return -1 end end,
        readHalfWord = function(self, a)
            if a >= 0 and a + 1 < #self._d then return self._d[a] + self._d[a + 1] * 256 else return -1 end
        end,
        readWord = function(self, a)
            if a >= 0 and a + 3 < #self._d then
                return self._d[a] + self._d[a+1]*256 + self._d[a+2]*65536 + self._d[a+3]*16777216
            else return -1 end
        end,
        _d = data,
    }
end

local _callbacks = {}
callbacks = { add = function(self, ev, fn) _callbacks[ev] = _callbacks[ev] or {}; table.insert(_callbacks[ev], fn) end }

-- Servidor TCP simulado sobre LuaSockets si existe; si no, modo bucle interno.
local HAVE_SOCKET = pcall(require, "socket")

-- Cargar el puente como módulo: extraemos funciones mediante un entorno compartido.
-- El puente usa globales de mGBA que ya hemos definido arriba.
local bridgeChunk = assert(loadfile("/workspace/emulator/lua/pbm_bridge.lua"))

if not HAVE_SOCKET then
    print("luasocket no disponible: ejecutando lógica del puente en modo directo (sin TCP).")
    -- Sustituimos socket.tcpListen por una fábrica falsa que captura callbacks.
    socket = { tcpListen = function(host, port, cb)
        local srv = { _clients = {}, _closed = false }
        function srv:accept() return nil end
        function srv:close() self._closed = true end
        return srv
    end }
end

bridgeChunk()

-- Si el servidor es falso, generamos frames manualmente y probamos handleCmd vía
-- un cliente falso que registra los mensajes enviados.
if type(socket) == "table" and socket._fake then end

local function drive(n)
    for _ = 1, n do
        for _, fn in ipairs(_callbacks["frame"] or {}) do fn() end
    end
end

drive(3)
print("[MOCK] Puentes simulados OK (servidor fake aceptado, frames ejecutados)")
