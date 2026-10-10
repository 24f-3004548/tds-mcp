import hashlib
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

EMAIL = "24f3004548@ds.study.iitm.ac.in".strip().lower()
app = FastAPI()

TOOL = {
    "name": "solve_challenge",
    "description": "Returns the exam response for the per-call challenge header.",
    "inputSchema": {"type": "object", "properties": {}},
}

def handle(msg, hdrs):
    mid, method = msg.get("id"), msg.get("method")
    if mid is None:                      # notification
        return None
    def ok(r): return {"jsonrpc": "2.0", "id": mid, "result": r}
    def err(c, m): return {"jsonrpc": "2.0", "id": mid, "error": {"code": c, "message": m}}

    if method == "initialize":
        pv = (msg.get("params") or {}).get("protocolVersion", "2025-03-26")
        return ok({"protocolVersion": pv,
                   "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": {"name": "exam-mcp", "version": "1.0.0"}})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": [TOOL]})
    if method == "tools/call":
        if (msg.get("params") or {}).get("name") != "solve_challenge":
            return err(-32602, "Unknown tool")
        ch = hdrs.get("x-exam-challenge", "").strip()
        if not ch:
            return ok({"content": [{"type": "text", "text": "missing X-Exam-Challenge"}], "isError": True})
        text = hashlib.sha256(f"{ch}:{EMAIL}".encode()).hexdigest()[:16]
        return ok({"content": [{"type": "text", "text": text}]})
    return err(-32601, "Method not found")

@app.post("/mcp")
async def mcp(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32700, "message": "Parse error"}}, status_code=400)
    hdrs = {k.lower(): v for k, v in request.headers.items()}
    if isinstance(body, list):
        out = [r for r in (handle(m, hdrs) for m in body) if r]
        return JSONResponse(out) if out else Response(status_code=202)
    r = handle(body, hdrs)
    return JSONResponse(r) if r else Response(status_code=202)

@app.get("/mcp")
async def mcp_get():
    return Response(status_code=405)

@app.get("/")
async def root():
    return {"ok": True}
