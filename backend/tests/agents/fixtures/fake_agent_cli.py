"""A stand-in agent CLI for the generic CLI adapter tests (never a real model).

Reads the prompt from STDIN and writes Claude ``stream-json`` lines to stdout. The prompt picks
the scenario:

* ``scenario:ok``        init, streamed text, a Read tool call + result, a final text, result
* ``scenario:hang``      starts a grandchild, writes both pids to $HOME/pids.txt, ignores
                         SIGINT and sleeps (the cancel test kills the whole group)
* ``scenario:noresult``  says something and exits 3 without a result line
* anything else          echoes the prompt as the answer
"""
import json
import os
import signal
import subprocess
import sys
import time


def out(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main():
    prompt = sys.stdin.read().strip()
    sid = "cli-sess-1"
    out({"type": "system", "subtype": "init", "session_id": sid, "mcp_servers": [], "tools": ["Read"]})
    if prompt == "scenario:hang":
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        child = subprocess.Popen([sys.executable, "-c",
                                  "import signal,time; signal.signal(signal.SIGINT, signal.SIG_IGN); time.sleep(600)"])
        with open(os.path.join(os.environ["HOME"], "pids.txt"), "w") as f:
            f.write(f"{os.getpid()} {child.pid}")
        out({"type": "assistant", "message": {"content": [{"type": "text", "text": "working..."}]}})
        time.sleep(600)
        return 0
    if prompt == "scenario:noresult":
        out({"type": "assistant", "message": {"content": [{"type": "text", "text": "oops"}]}})
        sys.stderr.write("fatal: something broke\n")
        return 3
    print("not json noise")
    if prompt == "scenario:ok":
        for piece in ("Let me ", "look."):
            out({"type": "stream_event", "event": {"type": "content_block_delta",
                                                   "delta": {"type": "text_delta", "text": piece}}})
        out({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Let me look."},
            {"type": "tool_use", "id": "tu_1", "name": "Read", "input": {"file_path": "README.md"}}]}})
        out({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "tu_1", "content": "hello from the file"}]}})
        out({"type": "assistant", "message": {"content": [{"type": "text", "text": "The file says hello."}]}})
        answer = "The file says hello."
    else:
        answer = f"echo: {prompt}"
        out({"type": "assistant", "message": {"content": [{"type": "text", "text": answer}]}})
    out({"type": "result", "subtype": "success", "is_error": False, "result": answer, "session_id": sid,
         "total_cost_usd": 0.0123, "num_turns": 2, "usage": {"input_tokens": 120, "output_tokens": 40}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
