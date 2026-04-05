local M = {}

-- nvim-dap adapter configuration for ZEsarUX (Z80)
-- ZEsarUX must be running with ZRCP enabled on port 10000

local plugin_root = vim.fn.fnamemodify(debug.getinfo(1, "S").source:sub(2), ":p:h:h:h:h")

M.setup = function(dap)
  dap.adapters.zesarux = {
    type = "executable",
    command = "python3",
    args = { plugin_root .. "/adapters/zesarux.py" },
  }

  dap.configurations.asm = {
    {
      type = "zesarux",
      request = "launch",
      name = "ZEsarUX Debug",
      preLaunchTask = "make build",
    },
  }
end

return M
