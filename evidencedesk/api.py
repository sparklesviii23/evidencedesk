"""Local REST API + web UI. Binds to 127.0.0.1. Every /api call needs the per-install token
(~/.evidencedesk/api_token, injected into the served UI) and a localhost Host header, which blocks cross-site
requests and DNS-rebinding attacks from web pages.

  GET  /api/operations            list of operations with parameters and docs
  POST /api/op/{operation}        call an operation with a JSON object of keyword arguments
  plus file routes for uploads, downloads, PRISMA SVG and zipped evidence packets.
"""
from __future__ import annotations

import inspect
import io
import json
import logging
import secrets
import shutil
import zipfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse

from . import engine, service
from .credentials import install_log_redaction, redact
from .db import data_dir
from .http import SourceError
from .query.parser import QuerySyntaxError

log = logging.getLogger("evidencedesk.api")
STATIC = Path(__file__).parent / "static"

# operations callable through /api/op (UI + agents). Credentials can be SET here (local UI) but never read.
OPERATIONS = [
    "list_reviews", "create_review", "get_review", "protocol_template", "save_protocol", "get_protocol",
    "list_protocol_versions", "draft_query_from_protocol", "validate_query", "preview_query", "complete_query", "grammar",
    "save_query", "list_saved_queries", "start_search", "search_status", "approve_source", "skip_source", "resume_search",
    "cancel_search", "list_runs", "run_dashboard", "rerun_saved_query", "run_diff", "import_records", "list_reports",
    "get_report", "get_record", "report_text", "dedup_candidates", "decide_candidate", "merge_reports", "split_record",
    "undo_merge", "merge_events", "list_studies", "create_study", "link_report_to_study", "unlink_report_from_study",
    "suggest_study_links", "decide_study_link", "add_cohort_link", "merge_studies", "retrieve_fulltext",
    "fulltext_summary", "upload_fulltext", "mark_fulltext_unavailable", "screening_queue", "propose_screening",
    "reviewer_decision", "adjudicate", "conflicts", "set_priority", "keyword_prioritize", "propose_extraction",
    "enter_extraction", "verify_extraction", "reject_extraction", "list_extraction", "propose_rob",
    "save_analysis_plan", "analysis_checks", "export_analysis", "prisma_counts", "prisma_s", "export_evidence_packet",
    "export_tables", "list_sources", "test_connection", "set_credential", "citation_chase", "benchmark_import",
    "benchmark_recall", "audit_log",
]


def token() -> str:
    p = data_dir() / "api_token"
    if not p.exists():
        p.write_text(secrets.token_urlsafe(32))
        p.chmod(0o600)
    return p.read_text().strip()


def describe_operations() -> list[dict]:
    out = []
    for name in OPERATIONS:
        fn = getattr(service, name)
        sig = inspect.signature(fn)
        params = []
        for p in sig.parameters.values():
            params.append({"name": p.name, "required": p.default is inspect.Parameter.empty,
                           "default": None if p.default is inspect.Parameter.empty else p.default,
                           "annotation": None if p.annotation is inspect.Parameter.empty else str(p.annotation)})
        out.append({"operation": name, "doc": (inspect.getdoc(fn) or "").strip(), "params": params})
    return out


def _json(obj, status=200):
    return Response(json.dumps(obj, default=str), status_code=status, media_type="application/json")


def create_app() -> FastAPI:
    install_log_redaction()
    app = FastAPI(title="EvidenceDesk", version="0.1.0", docs_url="/api/docs", openapi_url="/api/openapi.json")
    tok = token()

    @app.on_event("startup")
    def _startup():
        engine.resume_interrupted(service.db())
        from .fulltext import reclassify_media
        reclassify_media(service.db())

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = (request.headers.get("host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost", "[::1]", "testserver"):
            return JSONResponse({"error": "forbidden host"}, status_code=403)
        if request.url.path.startswith("/api/") and request.url.path not in ("/api/docs", "/api/openapi.json"):
            if request.headers.get("x-evidencedesk-token") != tok and request.query_params.get("token") != tok:
                return JSONResponse({"error": "missing or invalid X-EvidenceDesk-Token"}, status_code=401)
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (STATIC / "index.html").read_text().replace("__TOKEN__", tok)

    @app.get("/static/{name:path}")
    def static(name: str):
        p = (STATIC / name).resolve()
        if STATIC.resolve() not in p.parents or not p.exists():
            raise HTTPException(404)
        return FileResponse(p)

    @app.post("/api/agent/{name}")
    async def agent_tool(name: str, request: Request):
        """Lean agent tools (same as the default MCP tools); plain-text responses."""
        from . import agent
        fns = {f.__name__: f for f in agent.AGENT_TOOLS}
        if name not in fns:
            return Response(f"unknown agent tool '{name}'; tools: {', '.join(fns)}", status_code=404, media_type="text/plain")
        body = await request.body()
        args = json.loads(body) if body else {}
        import anyio
        try:
            res = await anyio.to_thread.run_sync(lambda: fns[name](**args))
        except QuerySyntaxError as e:
            return Response("SYNTAX ERROR " + e.message, status_code=400, media_type="text/plain")
        except (ValueError, KeyError, TypeError, SourceError) as e:
            return Response("ERROR " + redact(f"{type(e).__name__}: {e}"), status_code=400, media_type="text/plain")
        return Response(res if isinstance(res, str) else agent.compact(res), media_type="text/plain")

    @app.get("/api/operations")
    def ops():
        return _json(describe_operations())

    @app.post("/api/op/{name}")
    async def op(name: str, request: Request):
        if name not in OPERATIONS:
            return _json({"error": f"unknown operation {name}"}, 404)
        try:
            body = await request.json() if (await request.body()) else {}
        except json.JSONDecodeError:
            return _json({"error": "body must be JSON"}, 400)
        fn = getattr(service, name)
        try:
            import anyio
            res = await anyio.to_thread.run_sync(lambda: fn(**body))
            if name in ("prisma_s", "grammar"):
                return _json({"markdown": res})
            return _json({"ok": True, "result": res})
        except QuerySyntaxError as e:
            return _json({"ok": False, "error": e.message, "syntax_error": e.to_dict()}, 400)
        except (ValueError, KeyError, TypeError) as e:
            return _json({"ok": False, "error": redact(f"{type(e).__name__}: {e}")}, 400)
        except SourceError as e:
            return _json({"ok": False, "error": str(e), "kind": e.kind}, 502)
        except Exception as e:  # pragma: no cover
            log.exception("operation failed")
            return _json({"ok": False, "error": redact(f"{type(e).__name__}: {e}")}, 500)

    @app.get("/api/reviews/{rid}/prisma.svg")
    def prisma_svg(rid: int):
        return Response(service.prisma_svg(rid), media_type="image/svg+xml")

    @app.post("/api/reviews/{rid}/import")
    async def import_file(rid: int, file: UploadFile = File(...), platform: str = Form(...),
                          native_query: str = Form(""), search_date: str = Form(""), register: bool = Form(False)):
        import base64
        data = await file.read()
        try:
            res = service.import_records(rid, file_name=file.filename, content_b64=base64.b64encode(data).decode(),
                                         platform=platform, native_query=native_query or None,
                                         search_date=search_date or None, register=register)
        except ValueError as e:
            return _json({"ok": False, "error": str(e)}, 400)
        return _json({"ok": True, "result": res})

    @app.post("/api/reports/{rid}/upload")
    async def upload(rid: int, file: UploadFile = File(...), kind: str = Form("fulltext"), actor: str = Form("user")):
        import base64
        rep = service.get_report(rid)
        data = await file.read()
        res = service.upload_fulltext(rep["review_id"], rid, file.filename, base64.b64encode(data).decode(), kind, actor)
        return _json({"ok": True, "result": res})

    @app.get("/api/files/{fid}")
    def get_file(fid: int):
        f = service.db().one("SELECT * FROM files WHERE id=?", (fid,))
        if not f or not Path(f["path"]).exists():
            raise HTTPException(404)
        return FileResponse(f["path"], media_type=f["mime"], filename=Path(f["path"]).name)

    @app.get("/api/reviews/{rid}/packet.zip")
    def packet_zip(rid: int, scope: str = "full_text_stage", full_text: bool = True):
        res = service.export_evidence_packet(rid, scope=scope, include_full_text=full_text)
        out = Path(res["out_dir"])
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for p in out.rglob("*"):
                if p.is_file():
                    z.write(p, Path(out.name) / p.relative_to(out))
        buf.seek(0)
        return StreamingResponse(buf, media_type="application/zip",
                                 headers={"Content-Disposition": f'attachment; filename="{out.name}.zip"'})

    @app.get("/api/reviews/{rid}/analysis.zip")
    def analysis_zip(rid: int):
        res = service.export_analysis(rid)
        out = Path(service._export_root(rid) / "analysis")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for p in out.rglob("*"):
                if p.is_file():
                    z.write(p, Path("analysis") / p.relative_to(out))
        buf.seek(0)
        return StreamingResponse(buf, media_type="application/zip",
                                 headers={"Content-Disposition": 'attachment; filename="analysis.zip"'})

    @app.get("/api/analysis/{rid}/{name}")
    def analysis_file(rid: int, name: str):
        base = (service._export_root(rid) / "analysis").resolve()
        p = (base / name).resolve()
        if base not in p.parents or not p.exists():
            raise HTTPException(404)
        return FileResponse(p)

    return app


app = None


def get_app():
    global app
    if app is None:
        app = create_app()
    return app
