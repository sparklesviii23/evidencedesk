"""Command line: serve the UI/API, run the MCP server, manage credentials, test connectors."""
from __future__ import annotations

import argparse
import getpass
import json
import webbrowser


def main(argv=None):
    ap = argparse.ArgumentParser(prog="evidencedesk")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="start the local web UI + REST API")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--no-browser", action="store_true")
    sub.add_parser("mcp", help="run the MCP server on stdio")
    c = sub.add_parser("set-credential", help="store a credential in the OS keychain (prompted, not echoed)")
    c.add_argument("source")
    c.add_argument("key")
    c.add_argument("--clear", action="store_true")
    sub.add_parser("test-connections", help="run every connector's connection test")
    sub.add_parser("token", help="print the local API token (for agents using the REST API)")
    ag = sub.add_parser("agent", help="call a lean agent tool, e.g. `evidencedesk agent status 1` or "
                                      "`evidencedesk agent decide '{\"review_id\":1,\"stage\":\"ta\",\"decisions\":[...]}'`")
    ag.add_argument("tool")
    ag.add_argument("args", nargs="*", help="a JSON object, or positional values")
    a = ap.parse_args(argv)
    import faulthandler
    import signal
    faulthandler.register(signal.SIGUSR1, all_threads=True)  # `kill -USR1 <pid>` dumps all thread stacks
    if a.cmd == "serve":
        import uvicorn
        from .api import get_app, token
        url = f"http://127.0.0.1:{a.port}/"
        print(f"EvidenceDesk on {url}  (API token in ~/.evidencedesk/api_token)")
        if not a.no_browser:
            webbrowser.open(url)
        uvicorn.run(get_app(), host="127.0.0.1", port=a.port, log_level="info")
    elif a.cmd == "mcp":
        from .mcp_server import main as m
        m()
    elif a.cmd == "set-credential":
        from . import service
        from .credentials import CREDENTIAL_FIELDS
        fields = {f["key"]: f for f in CREDENTIAL_FIELDS.get(a.source, [])}
        if a.key not in fields:
            raise SystemExit(f"unknown credential {a.source}.{a.key}; known: "
                             + ", ".join(f"{s}.{f['key']}" for s, fs in CREDENTIAL_FIELDS.items() for f in fs))
        val = None if a.clear else (getpass.getpass(f"{fields[a.key]['label']}: ") if fields[a.key]["secret"]
                                    else input(f"{fields[a.key]['label']}: "))
        print(json.dumps(service.set_credential(a.source, a.key, val)))
    elif a.cmd == "test-connections":
        from . import service
        from .adapters import ADAPTERS
        for name in ADAPTERS:
            r = service.test_connection(name)
            print(f"{name:14} {'OK ' if r['ok'] else 'FAIL'} {r.get('detail', '')[:150]}")
    elif a.cmd == "agent":
        import inspect
        from . import agent, jobs, service
        fns = {f.__name__: f for f in agent.AGENT_TOOLS}
        if a.tool not in fns:
            raise SystemExit(f"unknown tool {a.tool}; tools: {', '.join(fns)}")
        fn = fns[a.tool]
        jobs.init(service.db())
        if len(a.args) == 1 and a.args[0].lstrip().startswith("{"):
            kwargs = json.loads(a.args[0])
        else:
            names = list(inspect.signature(fn).parameters)
            kwargs = {}
            for n, v in zip(names, a.args):
                try:
                    kwargs[n] = json.loads(v)
                except ValueError:
                    kwargs[n] = v
        from .http import SourceError
        from .query.parser import QuerySyntaxError
        try:
            res = fn(**kwargs)
        except QuerySyntaxError as e:
            raise SystemExit("SYNTAX ERROR " + e.message)
        except (ValueError, KeyError, TypeError, SourceError) as e:
            raise SystemExit(f"ERROR {type(e).__name__}: {e}")
        print(res if isinstance(res, str) else agent.compact(res))
        # background work started from the CLI must finish before the process exits
        import threading
        for t in threading.enumerate():
            if t.name.startswith(("job-", "run-")):
                t.join()
    elif a.cmd == "token":
        from .api import token
        print(token())


if __name__ == "__main__":
    main()
