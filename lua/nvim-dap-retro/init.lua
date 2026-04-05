local M = {}

local dapui_layout = {
  {
    elements = {
      { id = "repl",        size = 0.05 },
      { id = "scopes",      size = 0.25 },
      { id = "breakpoints", size = 0.25 },
      { id = "stacks",      size = 0.25 },
      { id = "watches",     size = 0.20 },
    },
    size = 40,
    position = "left",
  },
  {
    elements = { "memory_dump" },
    size = 10,
    position = "bottom",
  },
}

M.ext_map = {
  z80       = "zesarux",
  s80       = "zesarux",
  a         = "vice",
  s         = "vice",
  ["65s"]   = "vice",
  asm       = "zesarux",
}

M.setup = function(opts)
  opts = opts or {}
  M.ext_map = vim.tbl_extend("force", M.ext_map, opts.ext_map or {})

  local dap = require("dap")
  require("nvim-dap-retro.adapters.zesarux").setup(dap)
  require("nvim-dap-retro.adapters.vice").setup(dap)
  require("nvim-dap-retro.adapters.mame").setup(dap)

  local memory = require("nvim-dap-retro.memory")
  memory.setup(opts.memory or {})

  local ok, dapui = pcall(require, "dapui")
  if ok then
    dapui.register_element("memory_dump", {
      render = function()
        memory.refresh()
      end,
      buffer = function()
        return memory.buf()
      end,
    })
    dapui.setup({ layouts = dapui_layout })

    dap.listeners.after.event_stopped["nvim-dap-retro"] = function()
      dapui.open()
    end
  end
  vim.notify("nvim-dap-retro loaded", vim.log.levels.INFO)
end

local function find_tasks_file()
  local primary  = vim.fn.getcwd() .. "/.debug/tasks.json"
  local fallback = vim.fn.getcwd() .. "/.vscode/tasks.json"
  if vim.fn.filereadable(primary) == 1 then return primary end
  if vim.fn.filereadable(fallback) == 1 then return fallback end
end

local function resolve_task(label)
  local path = find_tasks_file()
  if not path then return nil end

  local ok, content = pcall(vim.fn.readfile, path)
  if not ok then return nil end

  local ok2, parsed = pcall(vim.fn.json_decode, table.concat(content, "\n"))
  if not ok2 then return nil end

  for _, task in ipairs(parsed.tasks or {}) do
    if task.label == label then
      return task
    end
  end
end

local function run_task(task, on_success)
  local args = task.args or {}
  local cmd = #args > 0 and vim.list_extend({ task.command }, args) or task.command
  vim.notify("nvim-dap-retro: running task '" .. task.label .. "'")
  vim.fn.jobstart(cmd, {
    on_exit = function(_, exit_code)
      if exit_code ~= 0 then
        vim.notify("nvim-dap-retro: task '" .. task.label .. "' failed (exit code " .. exit_code .. ")", vim.log.levels.ERROR)
        return
      end
      on_success()
    end,
  })
end

local function load_debug_launch_config(adapter)
  local path = vim.fn.getcwd() .. "/.debug/launch.json"
  if vim.fn.filereadable(path) == 0 then return nil end
  local ok, content = pcall(vim.fn.readfile, path)
  if not ok then return nil end
  local ok2, parsed = pcall(vim.fn.json_decode, table.concat(content, "\n"))
  if not ok2 then return nil end
  for _, c in ipairs(parsed.configurations or {}) do
    if c.type == adapter then return c end
  end
end

M.debug = function()
  local ext = vim.fn.expand("%:e")
  local adapter = M.ext_map[ext]
  if not adapter then
    vim.notify("nvim-dap-retro: no adapter for extension '." .. ext .. "'", vim.log.levels.WARN)
    return
  end

  local dap = require("dap")
  local config = load_debug_launch_config(adapter)
  if not config then
    for _, ft_configs in pairs(dap.configurations) do
      for _, c in ipairs(ft_configs) do
        if c.type == adapter then
          config = c
          break
        end
      end
      if config then break end
    end
  end

  if not config then
    vim.notify("nvim-dap-retro: no DAP configuration found for adapter '" .. adapter .. "'", vim.log.levels.WARN)
    return
  end

  if config.preLaunchTask then
    local task = resolve_task(config.preLaunchTask)
    if not task then
      vim.notify("nvim-dap-retro: task '" .. config.preLaunchTask .. "' not found in tasks.json", vim.log.levels.ERROR)
      return
    end
    run_task(task, function() dap.run(config) end)
  else
    dap.run(config)
  end
end

return M
