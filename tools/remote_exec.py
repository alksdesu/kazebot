from __future__ import annotations

"""
External tool (Clonoth).

The engine parses SPEC via AST at registration time.
At invocation this file runs as a subprocess:
  - Input: tool arguments as JSON on stdin
  - Output: result as JSON on stdout
  - Sensitive env vars are stripped
"""

SPEC = {'description': '在备用扫描服务器 (154.37.215.248) 上远程执行命令。通过 SSH ControlMaster '
                '长连接复用，延迟极低（~13ms）。用于扫描任务、文件操作等。',
 'input_schema': {'properties': {'command': {'description': '要在远程服务器上执行的 shell 命令',
                                             'type': 'string'},
                                 'timeout_sec': {'default': 30,
                                                 'description': '超时秒数，默认 30',
                                                 'type': 'number'}},
                  'required': ['command'],
                  'type': 'object'},
 'name': 'remote_exec'}

TIMEOUT_SEC = 35.0


if __name__ == "__main__":
    import json, sys
    _input = json.loads(sys.stdin.read())
    def output(result): print(json.dumps(result, ensure_ascii=False)); sys.exit(0)
    def fail(error):
        # [AutoC 2026-05-31] Why: failed external tools must still provide a
        # readable transcript under data.result. How: emit the unified ok/data/error
        # failure shape before exiting non-zero. Purpose: let the registry preserve
        # tool-specific errors even when the script exits with code 1.
        print(json.dumps({"ok": False, "error": str(error), "data": {"result": f"ERROR: {error}"}}, ensure_ascii=False)); sys.exit(1)
    args = _input
    import subprocess
    import sys
    
    cmd = args.get('command', '')
    timeout = int(args.get('timeout_sec', 30))
    
    if not cmd:
        fail('command is required')
    
    try:
        result = subprocess.run(
            ['ssh', 'scanner', cmd],
            capture_output=True, text=True,
            timeout=timeout
        )
        out = result.stdout
        err = result.stderr
        rc = result.returncode
        
        combined = ''
        if out:
            combined += out
        if err:
            if combined:
                combined += '\n--- stderr ---\n'
            combined += err
        
        if len(combined) > 50000:
            combined = combined[:50000] + '\n...(truncated)'
        
        # [AutoC 2026-05-31] Why: remote_exec is command-like and should match the
        # unified execute_command schema. How: keep returncode and output under data
        # and place the readable command transcript in data.result. Purpose: ensure
        # shell rc values are visible while ok reflects the tool contract.
        output({'ok': True, 'data': {'result': f'returncode={rc}\n{combined}', 'returncode': rc, 'output': combined}})
    except subprocess.TimeoutExpired:
        fail(f'Command timed out after {timeout}s')
    except Exception as e:
        fail(str(e))
