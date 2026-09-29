"""HTTP API for the reporting bot.

POST /chat          {"message": "...", "session_id": optional, "tenant": optional}
                    -> the turn (answer, sql, columns, rows, resolved request, ...) + session_id
POST /sessions/{id}/reset
GET  /sessions/{id} -> conversation history
GET  /health, GET /tenants, GET / (minimal web chat)
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict

import hmac

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .config import Settings
from .engine import ReportingEngine, ReportingSession

MAX_SESSIONS = 500
SESSION_TTL_S = 6 * 3600


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    session_id: str | None = None
    tenant: str | None = None


class SessionStore:
    def __init__(self, engine: ReportingEngine):
        self.engine = engine
        self._items: OrderedDict[str, tuple[float, ReportingSession, threading.Lock]] = OrderedDict()
        self._lock = threading.Lock()

    def get_or_create(self, session_id: str | None, tenant: str | None):
        with self._lock:
            now = time.time()
            for sid in [k for k, (ts, _, _) in self._items.items() if now - ts > SESSION_TTL_S]:
                self._items.pop(sid, None)
            if session_id and session_id in self._items:
                _, sess, lock = self._items.pop(session_id)
                self._items[session_id] = (now, sess, lock)
                return sess, lock
            sess = self.engine.session(tenant)
            if session_id:
                sess.id = session_id
            lock = threading.Lock()
            self._items[sess.id] = (now, sess, lock)
            while len(self._items) > MAX_SESSIONS:
                self._items.popitem(last=False)
            return sess, lock

    def get(self, session_id: str) -> ReportingSession | None:
        item = self._items.get(session_id)
        return item[1] if item else None


def create_app(settings: Settings | None = None, engine: ReportingEngine | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    engine = engine or ReportingEngine(settings)
    store = SessionStore(engine)
    app = FastAPI(title="Atlas AI Reporting Bot", version="1.0")

    def require_token(request: Request) -> None:
        """Bearer-token auth when ATLAS_API_TOKEN is set. The page reads its own data, so the
        token may also arrive as ?token= for the browser UI. Compared in constant time."""
        expected = settings.api_token
        if not expected:
            return  # no token configured: localhost development only
        header = request.headers.get("authorization", "")
        supplied = header[7:] if header.lower().startswith("bearer ") else (
            request.query_params.get("token") or request.headers.get("x-api-token") or "")
        if not hmac.compare_digest(supplied, expected):
            raise HTTPException(401, "missing or invalid API token")

    auth = [Depends(require_token)]

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "tables": len(engine.catalog.schema.tables),
                "llm": f"{engine.llm.name}/{getattr(engine.llm, 'model', '')}",
                "auth_required": bool(settings.api_token),
                "audit_log": str(settings.audit_log) if settings.audit_log else None}

    @app.get("/tenants", dependencies=auth)
    def tenants() -> list[dict]:
        return [{"slug": t["slug"], "name": t["name"]} for t in engine.executor.directory()]

    # Plain `def` endpoints run in FastAPI's threadpool, so a slow LLM call does not block others.
    @app.post("/chat", dependencies=auth)
    def chat(req: ChatRequest) -> dict:
        try:
            sess, lock = store.get_or_create(req.session_id, req.tenant)
        except Exception as e:
            raise HTTPException(400, str(e)) from e
        with lock:  # turns within one conversation are strictly sequential
            turn = sess.ask(req.message)
        body = turn.to_dict()
        body.update(session_id=sess.id, turn_number=len(sess.turns), company=sess.company)
        return body

    @app.get("/sessions/{session_id}", dependencies=auth)
    def history(session_id: str) -> dict:
        sess = store.get(session_id)
        if sess is None:
            raise HTTPException(404, "unknown session")
        return {"session_id": sess.id, "company": sess.company,
                "turns": [t.to_dict() for t in sess.turns]}

    @app.post("/sessions/{session_id}/reset", dependencies=auth)
    def reset(session_id: str) -> dict:
        sess = store.get(session_id)
        if sess is None:
            raise HTTPException(404, "unknown session")
        sess.reset()
        return {"session_id": session_id, "reset": True}

    @app.get("/", response_class=HTMLResponse, dependencies=auth)
    def index() -> str:
        return _INDEX_HTML

    return app


_INDEX_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Atlas Reporting Bot</title>
<style>body{font-family:system-ui,sans-serif;max-width:960px;margin:0 auto;padding:16px;background:#fafafa;color:#222}
.msg{margin:12px 0;padding:10px 14px;border-radius:8px;background:#fff;border:1px solid #e3e3e3}
.u{background:#eef4ff}.sql{font-family:monospace;font-size:12px;white-space:pre-wrap;background:#f4f4f4;padding:8px;border-radius:6px}
table{border-collapse:collapse;font-size:13px;margin-top:8px;display:block;overflow-x:auto}td,th{border:1px solid #ddd;padding:3px 8px;text-align:left}
form{display:flex;gap:8px;position:sticky;bottom:0;background:#fafafa;padding:8px 0}input{flex:1;padding:10px;font-size:15px}
.meta{color:#777;font-size:12px}</style></head><body>
<h2>Atlas AI Reporting Bot</h2><div class="meta">Company: <select id="tenant"></select> <button onclick="sid=null;log.innerHTML=''">New conversation</button></div>
<div id="log"></div><form id="f"><input id="q" placeholder="e.g. Show total sales in 2025" autofocus><button>Ask</button></form>
<script>
let sid=null;const log=document.getElementById('log');
// Forward the ?token= the page was opened with, so an authenticated deployment works in-browser.
const TOK=(new URLSearchParams(location.search).get('token')?'?token='+encodeURIComponent(new URLSearchParams(location.search).get('token')):'');
fetch('/tenants'+TOK).then(r=>r.json()).then(ts=>{const s=document.getElementById('tenant');ts.forEach(t=>{const o=document.createElement('option');o.value=t.slug;o.textContent=t.name+' ('+t.slug+')';s.appendChild(o)});s.onchange=()=>{sid=null;log.innerHTML=''}});
function esc(s){return String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
document.getElementById('f').onsubmit=async e=>{e.preventDefault();const q=document.getElementById('q');const m=q.value.trim();if(!m)return;q.value='';
log.insertAdjacentHTML('beforeend','<div class="msg u">'+esc(m)+'</div>');const w=document.createElement('div');w.className='msg';w.textContent='thinking...';log.appendChild(w);
const r=await fetch('/chat'+TOK,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:m,session_id:sid,tenant:document.getElementById('tenant').value})});
const d=await r.json();sid=d.session_id;let h='<div>'+esc(d.answer).replace(/\\n/g,'<br>')+'</div>';
if(d.sql)h+='<details><summary class="meta">SQL (turn '+d.turn_number+')</summary><div class="sql">'+esc(d.sql)+'</div></details>';
if(d.columns&&d.columns.length){h+='<table><tr>'+d.columns.map(c=>'<th>'+esc(c)+'</th>').join('')+'</tr>'+d.rows.slice(0,100).map(r=>'<tr>'+r.map(v=>'<td>'+esc(v)+'</td>').join('')+'</tr>').join('')+'</table>'}
w.innerHTML=h;window.scrollTo(0,document.body.scrollHeight)};
</script></body></html>"""
