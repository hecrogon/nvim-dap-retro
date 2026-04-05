local M = {}

-- nvim-dap adapter configuration for MAME (multi-system emulator)
-- MAME must be launched with: -debugger gdbstub -debug -debugger_port PORT
-- The adapter handles this automatically when mameArgs is set in launch.json.

M.setup = function(dap)
  dap.adapters.mame = {
    type = "executable",
    command = "python3",
    args = { vim.fn.expand("~/develop/retro/nvim-dap-retro/adapters/mame.py") },
  }
end

return M
