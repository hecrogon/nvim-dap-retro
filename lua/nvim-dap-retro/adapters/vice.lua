local M = {}

-- nvim-dap adapter configuration for VICE (6502/Commodore)
-- VICE must be running with binary monitor enabled

M.setup = function(dap)
  dap.adapters.vice = {
    type = "executable",
    command = "python3",
    args = { vim.fn.expand("~/develop/retro/nvim-dap-retro/adapters/vice.py") },
  }

  dap.configurations.asm = dap.configurations.asm or {}
  table.insert(dap.configurations.asm, {
    type = "vice",
    request = "launch",
    name = "VICE Debug",
  })
end

return M
