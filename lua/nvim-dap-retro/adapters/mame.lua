local M = {}

-- nvim-dap adapter configuration for MAME (multi-system emulator)
-- MAME must be launched with: -debugger gdbstub -debug -debugger_port PORT
-- The adapter handles this automatically when mameArgs is set in launch.json.

local plugin_root = vim.fn.fnamemodify(debug.getinfo(1, "S").source:sub(2), ":p:h:h:h:h")

M.setup = function(dap)
  dap.adapters.mame = {
    type = "executable",
    command = "python3",
    args = { plugin_root .. "/adapters/mame.py" },
  }
end

return M
