-- Install luarocks

local socket = require("socket")

local host = "localhost"
local port = 10000

local client = assert(socket.tcp())

client:connect(host, port)
client:settimeout(1)

local response, err = client:receive(200)

client:send("get-registers\n")
local response, err = client:receive("*l")
print(response)
print(err)

function send_command(client, command) 
  client:send(command .. "\n")
  local response, err = client:receive("*l")
  print(response)
  print(err)
end

send_command(client, "hard-reset-cpu")
send_command(client, "enter-cpu-step")
send_command(client, "load-binary /home/hector/develop/amstrad/04.helloworld/build/hello.bin 4000h 0")
send_command(client, "enable-breakpoints")
-- send_command(client, "set-breakpoint 1 PC=4000h")
-- send_command(client, "set-breakpoint 2 PC=4002h")
-- send_command(client, "set-breakpoint 3 PC=4005h")
-- send_command(client, "set-breakpoint 4 PC=4007h")
send_command(client, "disable-breakpoint 100")
send_command(client, "set-register PC=4000h")

client:close()
