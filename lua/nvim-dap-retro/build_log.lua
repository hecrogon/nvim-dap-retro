local M = {}

local buf = nil

M.setup = function()
  buf = vim.api.nvim_create_buf(false, true)
  vim.api.nvim_buf_set_name(buf, "Build Log")
  vim.api.nvim_set_option_value("modifiable", false,    { buf = buf })
  vim.api.nvim_set_option_value("buftype",    "nofile", { buf = buf })
end

M.clear = function()
  if not buf or not vim.api.nvim_buf_is_valid(buf) then return end
  vim.api.nvim_set_option_value("modifiable", true, { buf = buf })
  vim.api.nvim_buf_set_lines(buf, 0, -1, false, {})
  vim.api.nvim_set_option_value("modifiable", false, { buf = buf })
end

-- lines: list of strings as delivered by jobstart on_stdout/on_stderr callbacks.
-- jobstart always appends a trailing "" to mark chunk boundaries; we drop it and
-- join it with the previous last line so partial lines across callbacks are merged.
local pending = ""

M.append = function(lines)
  if not buf or not vim.api.nvim_buf_is_valid(buf) then return end
  if not lines or #lines == 0 then return end

  -- Merge pending partial line with the first element of the new chunk.
  local merged = {}
  merged[1] = pending .. (lines[1] or "")
  for i = 2, #lines do
    merged[i] = lines[i]
  end

  -- The last element is always "" in streaming mode; it represents the
  -- boundary between chunks (the real line may continue in the next call).
  if merged[#merged] == "" then
    pending = table.remove(merged) -- keep the empty slot as the new pending seed
  else
    pending = table.remove(merged) -- last element is an incomplete line
  end

  if #merged == 0 then return end

  vim.schedule(function()
    if not vim.api.nvim_buf_is_valid(buf) then return end
    vim.api.nvim_set_option_value("modifiable", true, { buf = buf })
    local count = vim.api.nvim_buf_line_count(buf)
    vim.api.nvim_buf_set_lines(buf, count, -1, false, merged)
    -- Auto-scroll every window that is showing this buffer.
    local new_count = vim.api.nvim_buf_line_count(buf)
    for _, win in ipairs(vim.api.nvim_list_wins()) do
      if vim.api.nvim_win_get_buf(win) == buf then
        vim.api.nvim_win_set_cursor(win, { new_count, 0 })
      end
    end
    vim.api.nvim_set_option_value("modifiable", false, { buf = buf })
  end)
end

M.flush = function()
  if pending ~= "" then
    M.append({ pending, "" })
    pending = ""
  end
end

M.buf = function() return buf end

return M
