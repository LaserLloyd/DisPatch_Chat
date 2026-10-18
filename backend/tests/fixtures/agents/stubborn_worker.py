"""A worker that ignores the close request, to test escalation (never touches anything else)."""
import json
import sys
import time

sys.stdin.buffer.readline()                       # the start command
sys.stdout.buffer.write((json.dumps({"kind": "ready"}) + "\n").encode())
sys.stdout.buffer.flush()
while True:
    time.sleep(0.2)
