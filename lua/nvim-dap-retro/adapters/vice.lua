local M = {}

-- nvim-dap adapter configuration for VICE (6502/Commodore)
-- VICE must be running with binary monitor enabled

local plugin_root = vim.fn.fnamemodify(debug.getinfo(1, "S").source:sub(2), ":p:h:h:h:h")

M.setup = function(dap)
  dap.adapters.vice = {
    type = "executable",
    command = "python3",
    args = { plugin_root .. "/adapters/vice.py" },
  }

  -- No dap.configurations.asm registration here: VICE is a 6502/Commodore
  -- adapter (still an unimplemented stub) and shouldn't show up as a
  -- debug option for Z80/CPC .asm files via nvim-dap's built-in config
  -- picker. Per-project .debug/launch.json configs are unaffected.
end

return M
