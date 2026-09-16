local M = {}

M.dapui_layout = {
  {
    elements = {
      { id = "repl",        size = 0.15 },
      { id = "scopes",      size = 0.35 },
      { id = "breakpoints", size = 0.15 },
      { id = "stacks",      size = 0.20 },
      { id = "watches",     size = 0.15 },
    },
    size = 40,
    position = "left",
  },
  {
    elements = { "memory_dump" },
    size = 10,
    position = "bottom",
  },
  {
    elements = { "build_log" },
    size = 10,
    position = "bottom",
  },
}

M.ext_map = {
  z80       = "zesarux",
  s80       = "zesarux",
  c         = "zesarux",
  a         = "vice",
  s         = "zesarux",  -- vice.py isn't implemented yet, and .s is Z80 asm in our samples anyway
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

  local memory    = require("nvim-dap-retro.memory")
  local build_log = require("nvim-dap-retro.build_log")
  memory.setup(opts.memory or {})
  build_log.setup()

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
    dapui.register_element("build_log", {
      render = function() end,
      buffer = function()
        return build_log.buf()
      end,
    })
    dap.listeners.after.event_stopped["nvim-dap-retro"] = function()
      dapui.open()
    end

    vim.api.nvim_create_autocmd("VimResized", {
      group = vim.api.nvim_create_augroup("nvim-dap-retro-resize", { clear = true }),
      callback = function()
        if dap.session() then
          dapui.open({ reset = true })
        end
      end,
    })
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
  local build_log = require("nvim-dap-retro.build_log")
  build_log.clear()

  local args = task.args or {}
  local cmd = #args > 0 and vim.list_extend({ task.command }, args) or task.command
  vim.notify("nvim-dap-retro: running task '" .. task.label .. "'")
  vim.fn.jobstart(cmd, {
    on_stdout = function(_, data) build_log.append(data) end,
    on_stderr = function(_, data) build_log.append(data) end,
    on_exit = function(_, exit_code)
      build_log.flush()
      if exit_code ~= 0 then
        vim.notify("nvim-dap-retro: task '" .. task.label .. "' failed (exit code " .. exit_code .. ")", vim.log.levels.ERROR)
        return
      end
      on_success()
    end,
  })
end

local function read_launch_configs()
  local path = vim.fn.getcwd() .. "/.debug/launch.json"
  if vim.fn.filereadable(path) == 0 then return nil end
  local ok, content = pcall(vim.fn.readfile, path)
  if not ok then return nil end
  local ok2, parsed = pcall(vim.fn.json_decode, table.concat(content, "\n"))
  if not ok2 then return nil end
  local configs = parsed.configurations or {}
  if #configs == 0 then return nil end
  return configs
end

local function start_config(dap, config)
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

M.debug = function()
  local dap = require("dap")
  local configs = read_launch_configs()

  -- If launch.json exists, let the user pick a configuration from it (VSCode-style)
  -- instead of guessing from the file extension -- a project might debug the same
  -- source with either ZEsarUX or MAME.
  if configs then
    if #configs == 1 then
      start_config(dap, configs[1])
    else
      vim.ui.select(configs, {
        prompt = "nvim-dap-retro: select a debug configuration",
        format_item = function(c) return c.name or c.type end,
      }, function(choice)
        if choice then start_config(dap, choice) end
      end)
    end
    return
  end

  -- No launch.json: fall back to the extension's default adapter and whatever
  -- config it registered in dap.configurations.
  local ext = vim.fn.expand("%:e")
  local adapter = M.ext_map[ext]
  if not adapter then
    vim.notify("nvim-dap-retro: no adapter for extension '." .. ext .. "'", vim.log.levels.WARN)
    return
  end

  local config
  for _, ft_configs in pairs(dap.configurations) do
    for _, c in ipairs(ft_configs) do
      if c.type == adapter then
        config = c
        break
      end
    end
    if config then break end
  end

  if not config then
    vim.notify("nvim-dap-retro: no DAP configuration found for adapter '" .. adapter .. "'", vim.log.levels.WARN)
    return
  end

  start_config(dap, config)
end

return M
