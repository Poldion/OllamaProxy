"""Nachgebautes Ollama (und ein Modul) fuer die Proxy-Tests.

Merkt sich die letzte Anfrage je Pfad in S["last"], damit die Tests pruefen koennen, was der Proxy
tatsaechlich an Ollama schickt. Antworten sind feste Beispiele, gesteuert ueber S (z.B. tool_calls).
"""
import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()
S: dict = {"last": {}, "tool_calls": None, "thinking": None, "module_freed": 0}

MODELS = {
    "gemma4:test": {"caps": ["completion", "vision", "tools"], "size": 9_000_000_000},
    "qwen:test": {"caps": ["completion", "tools", "thinking"], "size": 20_000_000_000},
    "nicht-freigegeben:latest": {"caps": ["completion"], "size": 1_000_000_000},
}


def reset():
    S.update(last={}, tool_calls=None, thinking=None, module_freed=0)


@app.get("/api/version")
async def version():
    return {"version": "0.0.0-test"}


@app.get("/api/tags")
async def tags():
    return {"models": [{"name": n, "model": n, "size": m["size"], "modified_at": "2026-01-01T00:00:00Z",
                        "details": {"family": n.split(":")[0]}} for n, m in MODELS.items()]}


@app.get("/api/ps")
async def ps():
    return {"models": [{"name": "gemma4:test", "model": "gemma4:test", "size": 10_000_000_000,
                        "size_vram": 10_000_000_000, "context_length": 64000,
                        "expires_at": "2099-01-01T00:00:00Z"}]}


@app.post("/api/show")
async def show(request: Request):
    body = await request.json()
    name = body.get("model") or body.get("name")
    if name not in MODELS:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"capabilities": MODELS[name]["caps"], "modelfile": f"FROM /blobs/{name}\n",
            "parameters": "", "model_info": {"general.architecture": "test"}}


def _message():
    m = {"role": "assistant", "content": "Hallo"}
    if S["thinking"]:
        m["thinking"] = S["thinking"]
    if S["tool_calls"]:
        m["tool_calls"] = S["tool_calls"]
        m["content"] = ""
    return m


@app.post("/api/chat")
async def chat(request: Request):
    body = await request.json()
    S["last"]["/api/chat"] = body
    done = {"model": body["model"], "done": True, "done_reason": "stop", "prompt_eval_count": 5, "eval_count": 3}
    if body.get("stream") is False:
        return {**done, "message": _message()}

    def gen():
        m = _message()
        if m.get("thinking"):
            yield json.dumps({"model": body["model"], "done": False,
                              "message": {"role": "assistant", "content": "", "thinking": m["thinking"]}}) + "\n"
        if m.get("tool_calls"):
            yield json.dumps({"model": body["model"], "done": False,
                              "message": {"role": "assistant", "content": "", "tool_calls": m["tool_calls"]}}) + "\n"
        else:
            for part in ("Hal", "lo"):
                yield json.dumps({"model": body["model"], "done": False,
                                  "message": {"role": "assistant", "content": part}}) + "\n"
        yield json.dumps({**done, "message": {"role": "assistant", "content": ""}}) + "\n"
    return StreamingResponse(gen(), media_type="application/x-ndjson")


@app.post("/api/generate")
async def generate(request: Request):
    body = await request.json()
    S["last"]["/api/generate"] = body
    return {"model": body["model"], "response": "Text", "done": True, "done_reason": "stop",
            "prompt_eval_count": 2, "eval_count": 1}


# ---- ein Modul (Schnittstelle /proxy-module) ----
@app.get("/proxy-module")
async def module_info():
    return {"name": "Testmodul", "version": "1", "links": [{"label": "Oeffnen", "path": "/"}],
            "capabilities": ["memory", "free"], "idle_free_minutes": 0}


@app.get("/proxy-module/memory")
async def module_memory():
    return {"rss": 2 * 1024 ** 3, "gpu": 3 * 1024 ** 3, "held": 5 * 1024 ** 3, "reclaimable": True,
            "busy": False, "detail": "2 Modelle im Cache"}


@app.post("/proxy-module/free")
async def module_free():
    S["module_freed"] += 1
    return {"ok": True}
