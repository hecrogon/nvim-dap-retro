local M = {}

local buf = nil
local load_address = 0x4000
local current_address = nil
local byte_count = 256
local _lines = {}
local _bytes = {}

local function hex_dump(bytes, start_addr)
  local lines = {}
  for i = 0, #bytes - 1, 16 do
    local hex_parts = {}
    local ascii_parts = {}
    for j = 0, 15 do
      local b = bytes[i + j + 1]
      if b then
        table.insert(hex_parts, string.format("%02X", b))
        if b >= 32 and b < 127 then
          table.insert(ascii_parts, string.char(b))
        else
          table.insert(ascii_parts, ".")
        end
      else
        table.insert(hex_parts, "  ")
        table.insert(ascii_parts, " ")
      end
    end
    table.insert(lines, string.format(
      "%04X: %-47s  %s",
      start_addr + i,
      table.concat(hex_parts, " "),
      table.concat(ascii_parts)
    ))
  end
  return lines
end

local function update(data, addr)
  local bytes = {}
  for i = 1, #data do
    bytes[i] = data:byte(i)
  end
  _bytes = bytes
  local lines = hex_dump(bytes, addr)
  _lines = lines
  if buf and vim.api.nvim_buf_is_valid(buf) then
    vim.api.nvim_set_option_value("modifiable", true, { buf = buf })
    vim.api.nvim_buf_set_lines(buf, 0, -1, false, lines)
    vim.api.nvim_set_option_value("modifiable", false, { buf = buf })
  end
  if M.on_update then M.on_update() end
end

local function refresh()
  local session = require("dap").session()
  if not session then return end

  if not current_address then
    local cfg = session.config
    current_address = load_address
    if cfg and cfg.loadAddress then
      current_address = type(cfg.loadAddress) == "number" and cfg.loadAddress
        or tonumber(cfg.loadAddress) or load_address
    end
  end

  local addr = current_address
  session:request("readMemory", {
    memoryReference = string.format("0x%04x", addr),
    count = byte_count,
  }, function(err, response)
    if err or not response then return end
    local data = vim.base64.decode(response.data)
    vim.schedule(function() update(data, addr) end)
  end)
end

local function byte_addr_at_cursor()
  local row, col = unpack(vim.api.nvim_win_get_cursor(0))
  -- Line format: "AAAA: HH HH HH ..."
  -- Hex area starts at col 6 (0-indexed). Each byte is 3 chars wide (HH + space).
  if col < 6 then return nil end
  local hex_col = col - 6
  local byte_idx = math.floor(hex_col / 3)
  if byte_idx >= 16 or hex_col % 3 == 2 then return nil end  -- on a space
  local flat_idx = (row - 1) * 16 + byte_idx + 1            -- 1-indexed into _bytes
  local addr = (current_address or load_address) + flat_idx - 1
  if addr > 0xFFFF then return nil end
  return addr, flat_idx
end

local function edit_byte()
  local addr, flat_idx = byte_addr_at_cursor()
  if not addr then return end
  local session = require("dap").session()
  if not session then return end
  local current = _bytes[flat_idx]
  local prompt = current
    and string.format("Value at %04X [%02X] (hex): ", addr, current)
    or  string.format("Value at %04X (hex): ", addr)
  vim.ui.input({ prompt = prompt }, function(input)
    if not input or input == "" then return end
    local trimmed = vim.trim(input)
    if trimmed:sub(1, 2):lower() == "0x" then
      trimmed = trimmed:sub(3)
    end
    local value = tonumber(trimmed, 16)
    if not value or value < 0 or value > 255 then
      vim.notify("Invalid byte value (expected 00-FF)", vim.log.levels.ERROR)
      return
    end
    session:request("writeMemory", {
      memoryReference = string.format("0x%04x", addr),
      data = vim.base64.encode(string.char(value)),
    }, function(err)
      if err then
        vim.notify("writeMemory failed: " .. tostring(err), vim.log.levels.ERROR)
        return
      end
      vim.schedule(refresh)
    end)
  end)
end

local function scroll(bytes)
  current_address = (current_address or load_address) + bytes
  current_address = math.max(0, math.min(0xFFFF - byte_count + 1, current_address))
  refresh()
end

M.setup = function(opts)
  opts = opts or {}
  load_address = opts.load_address or 0x4000
  byte_count = opts.count or 256

  local keymaps = vim.tbl_extend("force", {
    page_down = "<C-f>",
    page_up   = "<C-b>",
    line_down = "j",
    line_up   = "k",
    edit_byte = "e",
  }, opts.keymaps or {})

  buf = vim.api.nvim_create_buf(false, true)
  vim.api.nvim_buf_set_name(buf, "Memory Dump")
  vim.api.nvim_set_option_value("modifiable", false, { buf = buf })
  vim.api.nvim_set_option_value("buftype", "nofile", { buf = buf })

  vim.keymap.set("n", keymaps.page_down, function() scroll(byte_count) end,  { buffer = buf, desc = "Memory: page down" })
  vim.keymap.set("n", keymaps.page_up,   function() scroll(-byte_count) end, { buffer = buf, desc = "Memory: page up" })
  vim.keymap.set("n", keymaps.line_down, function() scroll(16) end,          { buffer = buf, desc = "Memory: line down" })
  vim.keymap.set("n", keymaps.line_up,   function() scroll(-16) end,         { buffer = buf, desc = "Memory: line up" })
  vim.keymap.set("n", keymaps.edit_byte, edit_byte,                          { buffer = buf, desc = "Memory: edit byte" })

  require("dap").listeners.after.event_stopped["nvim-dap-retro.memory"] = refresh
end

M.open = function()
  if not buf or not vim.api.nvim_buf_is_valid(buf) then return end
  for _, win in ipairs(vim.api.nvim_list_wins()) do
    if vim.api.nvim_win_get_buf(win) == buf then return end
  end
  vim.cmd("botright 16split")
  vim.api.nvim_win_set_buf(0, buf)
end

M.buf = function() return buf end
M.get_lines = function() return _lines end
M.on_update = nil

M.refresh = refresh
M.scroll = scroll

return M
