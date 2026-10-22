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

  -- Bundled fallback for projects with no .debug/launch.json. zesaruxArgs matters:
  -- without it the adapter never starts an emulator, it only attaches to whatever is
  -- already on the default port -- which means inheriting a previous session's state.
  -- Leaving zesaruxPort unset lets each session claim its own free port instead.
  dap.configurations.asm = {
    {
      type = "zesarux",
      request = "launch",
      name = "ZEsarUX Debug",
      preLaunchTask = "make build",
      zesaruxArgs = { "--machine", "cpc6128", "--snap-no-change-machine", "--disable-autoframeskip" },
    },
  }
end

return M
