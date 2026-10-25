# Execution notes

`die` terminates the agent and ends its session. `kill` terminates another process by
PID; `suspend` pauses that process. These commands have different targets and effects.

`shutdown` shuts down or reboots the machine. `sleep` and `sleep-until` change the
agent callback interval, so a longer interval can delay status and result updates.

An empty result does not prove that a command never executed. Inspect the existing
command ID and agent status before creating another operation. Recover command IDs
through bounded history and continue polling the same command after a timeout.
