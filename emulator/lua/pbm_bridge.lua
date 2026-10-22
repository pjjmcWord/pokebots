-- pbm_bridge.lua — Puente TCP entre mGBA y el backend de Pokémon Bot Manager.
-- Protocolo: JSON-line v1 (ver shared/schemas/protocol.schema.json).
-- Seguridad: escucha SOLO en localhost; la parada de emergencia bloquea toda
-- entrada automática hasta recibir 'emergency_release' explícito.
-- Compatibilidad: probado con la API de scripting de mGBA 0.10.x
-- (documentación oficial: https://mgba.io/docs/scripting.html)
--
-- Uso dentro de mGBA: Scripting > Scripts > pbm_bridge.lua
-- El script abre un servidor TCP en 127.0.0.1:42070 y espera al backend.

local PORT = 42070
local HOST = "127.0.0.1" -- NUNCA exponer a la red (requisito del proyecto)

-- ---------------------------------------------------------------- utilidades
local function jsonEncode(v)
  if v == nil then return "null" end
  local t = type(v)
  if t == "number" then
    if v ~= v then return "null" end -- NaN
    return tostring(v)
  elseif t == "boolean" then
    return v and "true" or "false"
  elseif t == "string" then
    local s = v:gsub("\\", "\\\\"):gsub('"', '\\"'):gsub("\n", "\\n"):gsub("\r", "\\r"):gsub("\t", "\\t")
    s = s:gsub("[\x00-\x1f]", function(c) return string.format("\\u%04x", c:byte()) end)
    return '"' .. s .. '"'
  elseif t == "table" then
    local isArray, out = true, {}
    local n = 0
    for k in pairs(v) do
      n = n + 1
      if type(k) ~= "number" then isArray = false end
    end
    if isArray then
      for _, x in ipairs(v) do out[#out + 1] = jsonEncode(x) end
      return "[" .. table.concat(out, ",") .. "]"
    else
      for k, x in pairs(v) do out[#out + 1] = jsonEncode(tostring(k)) .. ":" .. jsonEncode(x) end
      return "{" .. table.concat(out, ",") .. "}"
    end
  end
  return "null"
end

-- Parser mínimo de JSON suficiente para los mensajes del protocolo PBM v1.
-- Devuelve tabla con claves escalares (string/number/boolean) o nil si es inválido.
local function jsonDecode(s)
  if not s then return nil end
  local pos = 1
  local skipWs, parseValue, parseString, parseNumber, parseObject, parseArray
  skipWs = function()
    while pos <= #s and s:sub(pos, pos):match("%s") do pos = pos + 1 end
  end
  parseString = function()
    if s:sub(pos, pos) ~= '"' then return nil end
    pos = pos + 1
    local buf = {}
    while pos <= #s do
      local c = s:sub(pos, pos)
      if c == '"' then pos = pos + 1; return table.concat(buf) end
      if c == "\\" then
        local n = s:sub(pos + 1, pos + 1)
        local map = { n = "\n", r = "\r", t = "\t", b = "\b", f = "\f", ['"'] = '"', ["\\"] = "\\", ["/"] = "/" }
        if map[n] then buf[#buf + 1] = map[n]; pos = pos + 2
        elseif n == "u" then
          local hex = s:sub(pos + 2, pos + 5)
          local code = tonumber(hex, 16)
          if not code then return nil end
          buf[#buf + 1] = utf8 and utf8.char(code) or "?"
          pos = pos + 6
        else return nil end
      else
        buf[#buf + 1] = c; pos = pos + 1
      end
    end
    return nil
  end
  parseNumber = function()
    local numstr = s:match("^(-?%d+%.?%d*[eE]?[+-]?%d*)", pos)
    if not numstr then return nil end
    pos = pos + #numstr
    return tonumber(numstr)
  end
  parseValue = function()
    skipWs()
    local c = s:sub(pos, pos)
    if c == '"' then return parseString() end
    if c == "{" then return parseObject() end
    if c == "[" then return parseArray() end
    if s:sub(pos, pos + 3) == "true" then pos = pos + 4; return true end
    if s:sub(pos, pos + 4) == "false" then pos = pos + 5; return false end
    if s:sub(pos, pos + 3) == "null" then pos = pos + 4; return nil end
    return parseNumber()
  end
  parseObject = function()
    if s:sub(pos, pos) ~= "{" then return nil end
    pos = pos + 1
    local obj = {}
    skipWs()
    if s:sub(pos, pos) == "}" then pos = pos + 1; return obj end
    while true do
      skipWs()
      local key = parseString()
      if not key then return nil end
      skipWs()
      if s:sub(pos, pos) ~= ":" then return nil end
      pos = pos + 1
      obj[key] = parseValue()
      skipWs()
      local d = s:sub(pos, pos)
      if d == "," then pos = pos + 1
      elseif d == "}" then pos = pos + 1; return obj
      else return nil end
    end
  end
  parseArray = function()
    if s:sub(pos, pos) ~= "[" then return nil end
    pos = pos + 1
    local arr = {}
    skipWs()
    if s:sub(pos, pos) == "]" then pos = pos + 1; return arr end
    while true do
      arr[#arr + 1] = parseValue()
      skipWs()
      local d = s:sub(pos, pos)
      if d == "," then pos = pos + 1
      elseif d == "]" then pos = pos + 1; return arr
      else return nil end
    end
  end
  local okv = parseValue()
  skipWs()
  return okv
end

-- ------------------------------------------------------------------ estado
local server = nil
local client = nil
local recvBuf = ""
local seq = 0
local emergencyStop = false   -- parada de emergencia: bloquea TODAS las entradas
local statusEnabled = false
local lastRomLoaded = nil
local heldButtons = {}        -- botones pulsados por el puente (automáticos)
local pendingHolds = {}       -- {button=frames restantes}
local lastManualSnapshot = "" -- para detectar entrada física del usuario

local BUTTON_MAP = {
  A = "KEY_A", B = "KEY_B", SELECT = "KEY_SELECT", START = "KEY_START",
  RIGHT = "KEY_DRIGHT", LEFT = "KEY_DLEFT", UP = "KEY_DUP", DOWN = "KEY_DDOWN",
  R = "KEY_R", L = "KEY_L",
}

local function send(msg)
  if not client then return end
  local ok, err = client:send(jsonEncode(msg) .. "\n")
  if not ok then
    client:close()
    client = nil
    recvBuf = ""
    print("[PBM] cliente desconectado: " .. tostring(err))
  end
end

local function romInfo()
  -- En mGBA 0.10 la cabecera se obtiene vía emulator:gameName()/header expuesto
  -- por clientController; lo intentamos con pcall y devolvemos nil si no existe.
  local code, title = nil, nil
  pcall(function()
    local h = emulator.getHeader and emulator:getHeader()
    if h then
      code = h.gameCode or h.code
      title = h.gameTitle or h.title
    end
  end)
  if not code then
    pcall(function()
      local name = emulator.getGameName and emulator:getGameName()
      if name and #name >= 4 then code = name:sub(1, 4); title = name end
    end)
  end
  return code, title
end

local function isRomLoaded()
  -- Indicador robusto disponible sin ingeniería inversa: si hay core activo,
  -- view:width() devuelve 240; sin ROM, la llamada falla o devuelve 0.
  local vw = nil
  pcall(function() vw = view:width() end)
  return (vw ~= nil) and (tonumber(vw) or 0) > 0
end

-- ------------------------------------------------------------- comandos
local function releaseAllButtons()
  for name in pairs(heldButtons) do
    local key = BUTTON_MAP[name]
    if key then pcall(function() input:releaseKey(_G[key]) end) end
  end
  heldButtons = {}
  pendingHolds = {}
end

local function handleCmd(msg)
  local id = msg.id
  local action = msg.action
  if type(action) ~= "string" then
    send({ v = 1, t = "ack", id = id or 0, ok = false, reason = "cmd invalido" })
    return
  end

  -- La parada de emergencia tiene prioridad absoluta sobre cualquier entrada.
  if action == "emergency_stop" then
    emergencyStop = true
    releaseAllButtons()
    statusEnabled = true
    send({ v = 1, t = "ack", id = id, ok = true, reason = "parada de emergencia activa" })
    return
  end
  if action == "emergency_release" then
    emergencyStop = false
    send({ v = 1, t = "ack", id = id, ok = true, reason = "parada desactivada manualmente" })
    return
  end

  if emergencyStop then
    send({ v = 1, t = "ack", id = id, ok = false, reason = "parada_emergencia" })
    return
  end

  if action == "ping" then
    send({ v = 1, t = "ack", id = id, ok = true })
  elseif action == "status_on" then
    statusEnabled = true
    send({ v = 1, t = "ack", id = id, ok = true })
  elseif action == "press_button" or action == "release_button" or action == "hold_frame" then
    local keyName = BUTTON_MAP[msg.button]
    if not keyName then
      send({ v = 1, t = "ack", id = id, ok = false, reason = "boton desconocido" })
      return
    end
    local key = _G[keyName]
    if action == "press_button" then
      pcall(function() input:holdKey(key) end)
      heldButtons[msg.button] = true
    elseif action == "release_button" then
      pcall(function() input:releaseKey(key) end)
      heldButtons[msg.button] = nil
    else -- hold_frame: mantiene N frames y suelta automáticamente
      local frames = math.max(1, math.min(600, tonumber(msg.frames) or 5))
      pcall(function() input:holdKey(key) end)
      heldButtons[msg.button] = true
      pendingHolds[msg.button] = frames
    end
    send({ v = 1, t = "ack", id = id, ok = true })
  elseif action == "mem_read" then
    local region = string.upper(msg.region or "")
    local addr = tonumber(msg.address) or -1
    local width = tonumber(msg.width) or 1
    local value, error = nil, nil
    local readers = {
      IWRAM = memory.IWRAM, EWRAM = memory.EWRAM, ROM = memory.ROM,
      PALETTE = memory.PALETTE, VRAM = memory.VRAM, OAM = memory.OAM,
      SRAM = memory.SRAM, FLASH = memory.FLASH,
    }
    local mem = readers[region]
    if not mem then
      error = "region_desconocida"
    else
      local fn = ({ [1] = mem.readByte, [2] = mem.readHalfWord, [4] = mem.readWord })[width]
      if not fn then error = "ancho_invalido"
      else
        local ok, res = pcall(fn, mem, addr)
        if ok and res ~= nil and res >= 0 then value = res
        else error = "lectura_fuera_de_rango" end
      end
    end
    send({ v = 1, t = "memread", req = id, region = region, address = addr,
           width = width, value = value, error = error })
  elseif action == "set_speed" then
    -- Cambiar velocidad no está expuesto de forma fiable al scripting Lua en 0.10 SDL.
    send({ v = 1, t = "ack", id = id, ok = false, reason = "no_soportado_en_0.10_sdl" })
  else
    send({ v = 1, t = "ack", id = id, ok = false, reason = "accion_desconocida" })
  end
end

-- --------------------------------------------------------------- ciclo
local function acceptClient()
  if client then return end
  local ok, c = pcall(function() return server:accept() end)
  if ok and c then
    client = c
    recvBuf = ""
    local code, title = romInfo()
    seq = seq + 1
    send({ v = 1, t = "hello", mgba_version = VERSION or "desconocida",
           game_code = code, game_title = title, rom_loaded = isRomLoaded() })
    print("[PBM] cliente conectado desde localhost")
  end
end

local function pollClient()
  if not client then return end
  local ok, data = pcall(function() return client:recv() end)
  if not ok then
    client:close(); client = nil; recvBuf = ""
    return
  end
  if data and #data > 0 then
    recvBuf = recvBuf .. data
    while true do
      local nl = recvBuf:find("\n", 1, true)
      if not nl then break end
      local line = recvBuf:sub(1, nl - 1)
      recvBuf = recvBuf:sub(nl + 1)
      if #line > 0 then
        local msg = jsonDecode(line)
        if msg and msg.v == 1 then
          if msg.t == "cmd" then handleCmd(msg) end
        else
          send({ v = 1, t = "ack", id = 0, ok = false, reason = "mensaje_invalido_o_version_no_1" })
        end
      end
    end
  end
end

local function tickStatus()
  if not client or not statusEnabled then return end
  -- Suelta automático tras N frames (hold_frame)
  for name, frames in pairs(pendingHolds) do
    frames = frames - 1
    if frames <= 0 then
      pendingHolds[name] = nil
      local key = _G[BUTTON_MAP[name]]
      pcall(function() input:releaseKey(key) end)
      heldButtons[name] = nil
    else
      pendingHolds[name] = frames
    end
  end

  seq = seq + 1
  local held = {}
  for k in pairs(heldButtons) do held[#held + 1] = k end

  -- Detección de entrada manual: comparamos snapshot del teclado físico.
  -- En mGBA 0.10 SDL no hay callback de teclado expuesto a Lua; usamos
  -- keyboard.isPressed como aproximación y lo documentamos como limitación.
  local manualNow = ""
  pcall(function()
    for name, keyname in pairs(BUTTON_MAP) do
      local pressed = keyboard.isPressed and keyboard:isPressed(_G[keyname])
      if pressed then manualNow = manualNow .. name .. "," end
    end
  end)
  if manualNow ~= "" and manualNow ~= lastManualSnapshot then
    seq = seq + 1
    send({ v = 1, t = "event", seq = seq, kind = "manual_input", data = { buttons = manualNow } })
  end
  lastManualSnapshot = manualNow

  local loaded = isRomLoaded()
  if lastRomLoaded ~= nil and loaded ~= lastRomLoaded then
    seq = seq + 1
    send({ v = 1, t = "event", seq = seq, kind = loaded and "rom_loaded" or "rom_unloaded", data = {} })
  end
  lastRomLoaded = loaded

  send({ v = 1, t = "status", seq = seq, ts = os.time(), rom_loaded = loaded,
         paused = false, frame = frameCount or 0, buttons_held = held })
end

-- ------------------------------------------------------------- arranque
local okServer, srv = pcall(function()
  return socket.tcpListen(HOST, PORT, function(s) return s end)
end)

if not okServer or not srv then
  -- API alternativa en algunas compilaciones: sockets.server / listener
  local ok2, srv2 = pcall(function()
    return server ~= nil and server or nil
  end)
  print("[PBM] ERROR: no se pudo abrir el servidor TCP en " .. HOST .. ":" .. PORT)
  print("[PBM] Comprueba que tu build de mGBA incluye scripting con sockets (API socket).")
else
  server = srv
  print("[PBM] Servidor listo en " .. HOST .. ":" .. PORT .. " (solo localhost)")
end

frameCount = 0
callbacks:add("frame", function()
  frameCount = frameCount + 1
  if server then
    acceptClient()
    pollClient()
    tickStatus()
  end
end)

callbacks:add("shutdown", function()
  if client then pcall(function() client:close() end) end
  if server then pcall(function() server:close() end) end
end)
