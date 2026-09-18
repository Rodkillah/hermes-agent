import sys
print("sys.executable:", sys.executable)
import agent, gateway, hermes_cli, tools
import importlib
kt = importlib.import_module("tools.kanban_tools")
kb = importlib.import_module("hermes_cli.kanban_db")
kbw = importlib.import_module("gateway.kanban_watchers_notifier")
print("agent:", agent.__file__)
print("gateway:", gateway.__file__)
print("hermes_cli:", hermes_cli.__file__)
print("tools:", tools.__file__)
print("tools.kanban_tools:", kt.__file__)
print("hermes_cli.kanban_db:", kb.__file__)
print("gateway.kanban_watchers_notifier:", kbw.__file__)
