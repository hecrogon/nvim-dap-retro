local M = {}

local buf = nil
local load_address = 0x4000
local byte_count = 256
local _lines = {}

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
  local cfg = session.config
  local addr = load_address
  if cfg and cfg.loadAddress then
    addr = type(cfg.loadAddress) == "number" and cfg.loadAddress
      or tonumber(cfg.loadAddress) or load_address
  end
  session:request("readMemory", {
    memoryReference = string.format("0x%04x", addr),
    count = byte_count,
  }, function(err, response)
    if err or not response then return end
    local data = vim.base64.decode(response.data)
    vim.schedule(function() update(data, addr) end)
  end)
end

M.setup = function(opts)
  opts = opts or {}
  load_address = opts.load_address or 0x4000
  byte_count = opts.count or 256

  buf = vim.api.nvim_create_buf(false, true)
  vim.api.nvim_buf_set_name(buf, "Memory Dump")
  vim.api.nvim_set_option_value("modifiable", false, { buf = buf })
  vim.api.nvim_set_option_value("buftype", "nofile", { buf = buf })

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

return M
