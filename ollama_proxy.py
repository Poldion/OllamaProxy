#!/usr/bin/env python3
"""
OllamaProxy
===========

Sitzt zwischen deinen Clients (Open WebUI, Home Assistant, ...) und dem
Ollama-Server. Gibt eine feste Liste von Modellen frei, jedes mit EINEM festen
Satz reload-relevanter Optionen (num_ctx, keep_alive), damit Ollama pro Modell
genau einen Runner laedt und ihn dauerhaft im Speicher haelt -- egal welcher
Client mit welchen Settings fragt.

Modelle + Settings verwaltest du zentral ueber die Web-UI unter "/".
Alle Clients zeigen einfach auf diesen Proxy statt direkt auf Ollama.

Endpunkte:
  Native Ollama : /api/chat, /api/generate, /api/tags, /api/ps, /api/show, /api/version
  OpenAI-kompat.: /v1/chat/completions, /v1/completions (uebersetzt auf nativ),
                  /v1/models (mit context_length), /v1/embeddings
  Steuerung     : /            (Web-UI)
                  /admin/state  (GET aktuelle Config)
                  /admin/apply  (POST freigegebene Modelle setzen + warmup)
                  /admin/presets (GET/POST/DELETE)
                  /admin/memory  (GET RAM + geladene Modelle)
                  /admin/estimate (POST Prognose: passt Modell in den RAM?)
                  /admin/load, /admin/unload (POST Modell laden/entladen)

Start:
  pip install fastapi httpx uvicorn
  OLLAMA_UPSTREAM=http://mac-mini:11434 python ollama_proxy.py
"""

import base64
import hashlib
import json
import os
import re
import sys
import asyncio
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse, HTMLResponse, Response
from fastapi.middleware.cors import CORSMiddleware

import i18n
from i18n import N_

__version__ = "0.2.1"          # SemVer; einzige Stelle (Oberflaeche und /admin/state lesen sie von hier)

UPSTREAM = os.environ.get("OLLAMA_UPSTREAM", "http://localhost:11434").rstrip("/")
LISTEN_HOST = os.environ.get("PROXY_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("PROXY_PORT", "11435"))
STATE_FILE = Path(os.environ.get("PROXY_STATE", "./proxy_state.json"))

# ---------------------------------------------------------------------------
# Persistenter Zustand: freigegebene Modelle + gespeicherte Presets
# ---------------------------------------------------------------------------
#
# Mehrere Modelle koennen gleichzeitig "freigegeben" sein. Jedes hat eigene,
# fest erzwungene Settings. Fragt ein Client nach einem freigegebenen Modell
# (echter Name oder Alias), bekommt er genau dieses -- immer mit denselben
# Settings, egal welcher Client fragt. So muss Ollama kein Modell neu laden,
# nur weil zwei Clients verschiedene num_ctx mitschicken. Unbekannte Namen
# landen beim Standardmodell.

# Nur diese options-Keys erzwingt der Proxy hart -- sie sind es, die einen
# Runner-Reload ausloesen. Alles andere (temperature, top_p, stop, seed, ...)
# kommt vom Client und wird unangetastet durchgereicht.
# cache_type_k/v gehoerten frueher dazu, Ollama ignoriert sie pro Request aber
# (seit 0.40 sogar mit "invalid option provided"). Der KV-Cache-Typ gilt fuer
# den ganzen Ollama-Server (OLLAMA_KV_CACHE_TYPE), nicht pro Modell.
FORCED_OPTION_KEYS = ("num_ctx",)

DEFAULT_ENTRY = {
    "model": "gemma4:e4b",
    "alias": "",                       # leer = echter Name an Clients
    "forced_options": {"num_ctx": 8192},
    "keep_alive": -1,                  # -1 = dauerhaft geladen halten
    "think_default": None,             # None = nicht setzen; True/False = Default
    "warm": True,                      # beim Anwenden vorladen
    "profiles": [],                    # weitere Namen mit eigenen Sampling-Vorgaben (gleicher Runner)
}

# Profile (seit 2026-10-27): ein Modell unter weiteren Namen mit eigenen Sampling-Vorgaben, z.B.
# "Qwen3.6-Code" mit den Coding-Werten der Qwen-Autoren (temperature 0.6, presence_penalty 0) neben dem
# allgemeinen Modelfile-Stand (1.0 / 1.5). Sampling gilt je Anfrage -> kein Neuladen, derselbe Runner.
# Werte gelten nur, wenn der Client selbst keinen schickt (neutrale Strafwerte zaehlen als "keiner").
PROFILE_OPTION_KEYS = ("temperature", "top_p", "top_k", "min_p", "presence_penalty", "frequency_penalty",
                       "repeat_penalty")


def normalize_profile(p: dict) -> dict | None:
    alias = str(p.get("alias") or "").strip()
    if not alias:
        return None
    opts = {}
    for k in PROFILE_OPTION_KEYS:
        v = (p.get("options") or {}).get(k)
        if v in (None, ""):
            continue
        try:
            opts[k] = int(v) if k == "top_k" else float(v)
        except (TypeError, ValueError):
            continue
    think = p.get("think")
    return {"alias": alias, "options": opts, "think": think if think in (True, False) else None}

DEFAULT_STATE = {
    "models": [dict(DEFAULT_ENTRY)],
    "default_model": DEFAULT_ENTRY["model"],
    "vision_helper": "gemma4:e4b",     # beschreibt Bilder fuer reine Text-Modelle ("" = aus)
    "presets": {},                     # name -> {models, default_model}
}


def normalize_entry(e: dict) -> dict:
    """Eintrag vervollstaendigen und auf erlaubte Werte begrenzen."""
    out = json.loads(json.dumps(DEFAULT_ENTRY))
    out["model"] = (e.get("model") or "").strip()
    out["alias"] = (e.get("alias") or "").strip()
    fo = e.get("forced_options") or {}
    out["forced_options"] = {}
    for k in FORCED_OPTION_KEYS:
        if fo.get(k) not in (None, ""):
            out["forced_options"][k] = int(fo[k]) if k == "num_ctx" else fo[k]
    ka = e.get("keep_alive", -1)
    out["keep_alive"] = -1 if ka in (None, "", "-1") else ka
    td = e.get("think_default")
    out["think_default"] = td if td in (True, False, None) else None
    out["warm"] = bool(e.get("warm", True))
    out["profiles"] = [q for q in (normalize_profile(x) for x in e.get("profiles") or []
                                   if isinstance(x, dict)) if q]
    return out


def _legacy_entry(d: dict) -> dict:
    """Einzelmodell-Format (bis 2026-10) in einen Eintrag umwandeln."""
    return normalize_entry({
        "model": d.get("model") or DEFAULT_ENTRY["model"],
        "alias": d.get("client_model_name", ""),
        "forced_options": d.get("forced_options") or {},
        "keep_alive": d.get("keep_alive", -1),
        "think_default": d.get("think_default"),
        "warm": True,
    })


def migrate_state(data: dict) -> dict:
    """Altes Einzelmodell-Format und alte Presets ins Mehrmodell-Format heben."""
    if "models" not in data:
        e = _legacy_entry(data)
        data = {"models": [e], "default_model": e["model"],
                "presets": data.get("presets", {})}
    presets = {}
    for name, p in (data.get("presets") or {}).items():
        if "models" in p:
            presets[name] = {"models": [normalize_entry(e) for e in p["models"]],
                             "default_model": p.get("default_model")}
        else:
            e = _legacy_entry(p)
            presets[name] = {"models": [e], "default_model": e["model"]}
    data["models"] = [normalize_entry(e) for e in data.get("models") or []]
    if not data["models"]:
        data["models"] = [dict(DEFAULT_ENTRY)]
    if data.get("default_model") not in [e["model"] for e in data["models"]]:
        data["default_model"] = data["models"][0]["model"]
    if "vision_helper" not in data or data["vision_helper"] is None:
        data["vision_helper"] = DEFAULT_STATE["vision_helper"]
    else:
        data["vision_helper"] = str(data["vision_helper"]).strip()
    data["presets"] = presets
    return data


def validate_config(models: list, default_model: str) -> str | None:
    """Fehlertext oder None. Namen und Aliase muessen eindeutig zuordenbar sein."""
    if not models:
        return N_("mindestens ein Modell freigeben")
    names = set()
    for e in models:
        if not e["model"]:
            return N_("Eintrag ohne Modellname")
        for n in filter(None, (e["model"], e["alias"], *[q["alias"] for q in e.get("profiles", [])])):
            if n in names:
                return N_("Name/Alias '{x}' ist doppelt vergeben").replace("{x}", n)
            names.add(n)
    if default_model not in [e["model"] for e in models]:
        return N_("Standardmodell muss freigegeben sein")
    return None


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return migrate_state(json.loads(STATE_FILE.read_text()))
        except Exception:
            pass
    return json.loads(json.dumps(DEFAULT_STATE))


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


STATE = load_state()
client = httpx.AsyncClient(base_url=UPSTREAM, timeout=None)


def entries() -> list:
    return STATE["models"]


def default_entry() -> dict:
    return next(e for e in entries() if e["model"] == STATE["default_model"])


def _names(e: dict) -> tuple:
    return (e["model"], e["alias"], *[q["alias"] for q in e.get("profiles", [])])


def find_entry(name: str | None) -> dict | None:
    """Freigegebenen Eintrag zu echtem Namen, Alias oder Profilnamen finden (":latest" tolerant)."""
    n = (name or "").strip()
    if not n:
        return None
    for e in entries():
        if n in _names(e):
            return e
    # "llama3.2" == "llama3.2:latest"
    base = n[:-7] if n.endswith(":latest") else n + ":latest"
    for e in entries():
        if base in _names(e):
            return e
    return None


def find_profile(name: str | None) -> dict | None:
    """Profil zu einem Client-Namen (":latest" tolerant) oder None."""
    n = (name or "").strip()
    if not n:
        return None
    for cand in (n, n[:-7] if n.endswith(":latest") else n + ":latest"):
        for e in entries():
            for q in e.get("profiles", []):
                if q["alias"] == cand:
                    return q
    return None


def shown_name(requested: str | None, e: dict) -> str:
    """Name in Antworten: der angefragte Profilname, sonst Alias/echter Name des Eintrags."""
    q = find_profile(requested)
    return q["alias"] if q else client_name(e)


def resolve(name: str | None) -> dict:
    """Eintrag fuer eine Client-Anfrage: freigegebenes Modell oder Standard."""
    return find_entry(name) or default_entry()


def client_name(e: dict) -> str:
    """Name, den die Clients sehen: Alias falls gesetzt, sonst echter Name."""
    return e["alias"] or e["model"]

# ---------------------------------------------------------------------------
# Kern: eingehende Requests normalisieren
# ---------------------------------------------------------------------------

# Schleifen-Bremse der Modelle nicht versehentlich abschalten (2026-10-27): Qwen 3.6 bringt im Modelfile
# presence_penalty 1.5 mit (Empfehlung der Qwen-Autoren gegen Wiederholungs-/Denkschleifen). Viele
# Clients schicken aber ausdruecklich presence_penalty 0 / frequency_penalty 0 (Regler-Standard) - das
# ersetzt den Modelfile-Wert. Im Ollama-Log liefen 418 von 420 Qwen-/Flint-Anfragen mit 0 -> Schleifen.
# Neutrale Werte vom Client werden daher entfernt: dann gilt der Wert des Modells, und wo das Modell
# keinen hat, ist Ollamas Standard ohnehin derselbe neutrale Wert. Echte Werte (z.B. 0.5) bleiben.
NEUTRAL_PENALTIES = {"presence_penalty": 0.0, "frequency_penalty": 0.0, "repeat_penalty": 1.0}
_neutral_logged: set[tuple[str, str]] = set()


def drop_neutral_penalties(opts: dict, model: str):
    for key, neutral in NEUTRAL_PENALTIES.items():
        try:
            is_neutral = key in opts and float(opts[key]) == neutral
        except (TypeError, ValueError):
            continue
        if is_neutral:
            del opts[key]
            if (model, key) not in _neutral_logged:     # einmal je Modell und Feld, nicht pro Anfrage
                _neutral_logged.add((model, key))
                print(f"[sampling] {model}: neutrales {key}={neutral:g} vom Client entfernt -> Wert des Modells gilt",
                      flush=True)


def force_settings(body: dict) -> dict:
    """
    Modell aufloesen und nur reload-relevante Felder erzwingen. Client-options
    bleiben als Basis erhalten (Sampling, stop, ...), der Proxy legt nur die
    forced_options des Modells drueber. Top-Level-Felder (format, tools, seed,
    ...) bleiben unangetastet. Gilt fuer native und OpenAI-Endpunkte.
    """
    requested = body.get("model")
    e = resolve(requested)
    body["model"] = e["model"]
    body["keep_alive"] = e["keep_alive"]
    opts = dict(body.get("options") or {})        # Client-options als Basis
    drop_neutral_penalties(opts, e["model"])
    prof = find_profile(requested)
    if prof:                                       # Profil fuellt nur, was der Client nicht vorgibt
        for k, v in prof["options"].items():
            opts.setdefault(k, v)
        if "think" not in body and prof["think"] is not None:
            body["think"] = prof["think"]
    opts.update(e["forced_options"])               # nur diese ueberschreiben
    body["options"] = opts
    # think nur als Default auffuellen, wenn der Client selbst keins mitschickt.
    # Schickt der Client ein think (egal ob true/false), gewinnt der Client.
    if "think" not in body and e["think_default"] is not None:
        body["think"] = e["think_default"]
    return body


# frueher getrennt; OpenAI-Anfragen werden jetzt erst nach nativ uebersetzt
force_native = force_settings

# Obergrenze fuer die Antwortlaenge (num_predict). Ollama bearbeitet eine Anfrage nach der anderen
# (OLLAMA_NUM_PARALLEL=1): eine Antwort, die sich in einer Schleife verfaengt, blockierte am 2026-10-26
# fast 6 Minuten lang alle anderen (12.821 Token). Seit 2026-10-27 allgemein aus (Standard 0): 8192 schnitt
# Denkmodelle wie Qwen mitten im Denken ab, Wrapper schickten dann neu; die eigentliche Schleifen-Ursache
# (neutrale Strafwerte der Clients) behebt drop_neutral_penalties. PROXY_MAX_PREDICT gilt fuer alle Clients,
# PROXY_MAX_PREDICT_CLIENTS="IP=Anzahl,..." fuer einzelne (z.B. Home Assistant: kurze Sprachantworten).
# 0 = keine Grenze. Ein kleinerer Wert vom Client bleibt.
MAX_PREDICT = int(os.environ.get("PROXY_MAX_PREDICT", "0"))
MAX_PREDICT_CLIENTS = {ip.strip(): int(n) for ip, n in (
    item.split("=", 1) for item in os.environ.get("PROXY_MAX_PREDICT_CLIENTS", "").split(",") if "=" in item)}


def cap_predict(body: dict, client_host: str | None) -> dict:
    limit = MAX_PREDICT_CLIENTS.get(client_host or "", MAX_PREDICT)
    if limit > 0:
        opts = body.setdefault("options", {})
        try:
            n = int(opts.get("num_predict", -1))
        except (TypeError, ValueError):
            n = -1
        if n < 0 or n > limit:
            opts["num_predict"] = limit
    return body


def _client(request: Request) -> str | None:
    return request.client.host if request.client else None


async def stream_upstream(path: str, body: dict):
    """Request an Ollama weiterreichen und Antwort streamen."""
    req = client.build_request("POST", path, json=body)
    resp = await client.send(req, stream=True)

    async def gen():
        # finally: bricht der Client ab (z.B. Home Assistant nach "Stopp"), die Verbindung zu Ollama
        # schliessen - sonst rechnet Ollama die Antwort zu Ende und blockiert alle folgenden Anfragen
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
        finally:
            await resp.aclose()

    return StreamingResponse(
        gen(),
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "application/json"),
    )

# ---------------------------------------------------------------------------
# OpenAI -> nativ: Uebersetzung der /v1-Endpunkte
# ---------------------------------------------------------------------------
# Ollamas eigene OpenAI-Endpunkte ignorieren "options" (num_ctx), keep_alive und
# think -- ein OpenAI-Client wuerde das Modell mit dem Server-Standardkontext
# neu laden. Deshalb nimmt der Proxy OpenAI-Anfragen selbst an, uebersetzt sie auf
# /api/chat bzw. /api/generate (dort greifen alle erzwungenen Settings) und die
# Antwort zurueck ins OpenAI-Format -- so wie es Ollamas OpenAI-Schicht intern auch tut.

# OpenAI-Feld -> Ollama-option
_OAI_OPTIONS = {"temperature": "temperature", "top_p": "top_p", "seed": "seed",
                "frequency_penalty": "frequency_penalty",
                "presence_penalty": "presence_penalty"}


def _oai_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:24]}"


def _oai_options(b: dict) -> dict:
    opts = {o: b[k] for k, o in _OAI_OPTIONS.items() if b.get(k) is not None}
    max_t = b.get("max_completion_tokens") or b.get("max_tokens")
    if max_t:
        opts["num_predict"] = int(max_t)
    stop = b.get("stop")
    if stop:
        opts["stop"] = [stop] if isinstance(stop, str) else list(stop)
    # Ollama-Erweiterung: Client darf eigene options mitschicken (werden erzwungen
    # ueberschrieben, wo noetig)
    opts.update(b.get("options") or {})
    return opts


def _oai_think(b: dict):
    """reasoning_effort / reasoning.effort / think -> Ollama think (None = nicht setzen)."""
    if "think" in b:
        return b["think"]
    effort = b.get("reasoning_effort") or (b.get("reasoning") or {}).get("effort")
    if effort is None:
        return None
    return str(effort).lower() not in ("none", "minimal", "off", "false")


def _oai_format(b: dict):
    rf = b.get("response_format") or {}
    if rf.get("type") == "json_object":
        return "json"
    if rf.get("type") == "json_schema":
        return (rf.get("json_schema") or {}).get("schema") or "json"
    return None


def _oai_messages(msgs: list) -> list:
    """OpenAI-Nachrichten -> Ollama-Nachrichten (Text, Bilder, Audio, Tool-Aufrufe)."""
    out, tool_names = [], {}
    for m in msgs or []:
        role = m.get("role", "user")
        if role == "developer":
            role = "system"
        nm = {"role": role, "content": ""}
        content = m.get("content")
        if isinstance(content, list):
            texts, images = [], []
            for part in content:
                ptype = part.get("type")
                if ptype in ("text", "input_text"):
                    texts.append(part.get("text", ""))
                elif ptype in ("image_url", "input_image"):
                    url = part.get("image_url")
                    url = url.get("url") if isinstance(url, dict) else url
                    if isinstance(url, str) and url.startswith("data:") and "," in url:
                        images.append(url.split(",", 1)[1])
                    elif isinstance(url, str) and url and not url.startswith(("http://", "https://")):
                        images.append(url)
                elif ptype in ("input_audio", "audio", "audio_url"):
                    aud = part.get("input_audio") or part.get("audio") or part.get("audio_url")
                    data = aud.get("data") or aud.get("url") if isinstance(aud, dict) else aud
                    if isinstance(data, str) and data:
                        if data.startswith("data:") and "," in data:
                            data = data.split(",", 1)[1]
                        images.append(data)
            nm["content"] = "\n".join(texts)
            if images:
                nm["images"] = images
        elif content is not None:
            nm["content"] = content
        # Falls ein Client Audio separat auf Nachrichten-Ebene mitschickt (audio / audios)
        for akey in ("audio", "audios"):
            aval = m.get(akey)
            if aval:
                alist = [aval] if isinstance(aval, str) else list(aval)
                nm.setdefault("images", []).extend(
                    a.split(",", 1)[1] if isinstance(a, str) and a.startswith("data:") and "," in a else a
                    for a in alist if isinstance(a, str) and a
                )
        if m.get("tool_calls"):
            calls = []
            for tc in m["tool_calls"]:
                fn = tc.get("function") or {}
                args = fn.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args) if args.strip() else {}
                    except ValueError:
                        args = {"_raw": args}
                tool_names[tc.get("id")] = fn.get("name")
                call = {"function": {"name": fn.get("name"), "arguments": args or {}}}
                if tc.get("id"):
                    call["id"] = tc["id"]
                calls.append(call)
            nm["tool_calls"] = calls
        if role == "tool":
            name = m.get("name") or tool_names.get(m.get("tool_call_id"))
            if name:
                nm["tool_name"] = name
            if m.get("tool_call_id"):
                nm["tool_call_id"] = m["tool_call_id"]
        out.append(nm)
    return out


def _oai_tools(b: dict):
    if (b.get("tool_choice") or "") == "none":
        return None
    return b.get("tools") or None


def oai_chat_to_native(b: dict) -> dict:
    native = {"model": b.get("model"), "messages": _oai_messages(b.get("messages")),
              "stream": bool(b.get("stream")), "options": _oai_options(b)}
    for key, val in (("format", _oai_format(b)), ("tools", _oai_tools(b)),
                     ("think", _oai_think(b)), ("keep_alive", b.get("keep_alive"))):
        if val is not None:
            native[key] = val
    return native


def oai_completion_to_native(b: dict) -> dict:
    prompt = b.get("prompt", "")
    if isinstance(prompt, list):
        prompt = "".join(p for p in prompt if isinstance(p, str))
    native = {"model": b.get("model"), "prompt": prompt, "stream": bool(b.get("stream")),
              "options": _oai_options(b)}
    if b.get("suffix"):
        native["suffix"] = b["suffix"]
    fmt = _oai_format(b)
    if fmt is not None:
        native["format"] = fmt
    return native


def _oai_tool_calls(calls: list, start: int = 0) -> list:
    """Ollama-Tool-Aufrufe -> OpenAI. Ollama liefert jeden Aufruf komplett in einem Chunk
    (mit eigener ID `call_xxx`); diese ID wird uebernommen, damit sie im naechsten Request
    als `tool_call_id` wieder zum richtigen Aufruf passt. Nur wenn sie fehlt, eine neue."""
    out = []
    for i, tc in enumerate(calls or []):
        fn = tc.get("function") or {}
        args = fn.get("arguments")
        if not isinstance(args, str):
            args = json.dumps(args or {}, ensure_ascii=False)
        out.append({"index": start + i,
                    "id": tc.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                    "type": "function",
                    "function": {"name": fn.get("name"), "arguments": args}})
    return out


def _oai_finish(native: dict, had_tools: bool) -> str:
    if had_tools:
        return "tool_calls"
    return "length" if native.get("done_reason") == "length" else "stop"


def _oai_usage(native: dict) -> dict:
    p, c = native.get("prompt_eval_count") or 0, native.get("eval_count") or 0
    return {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}


def _oai_error(status: int, text: str) -> JSONResponse:
    try:
        msg = json.loads(text).get("error", text)
    except ValueError:
        msg = text
    return JSONResponse({"error": {"message": msg, "type": "api_error", "code": status}},
                        status_code=status)


async def _oai_call(path: str, native: dict, client_model: str, kind: str,
                    include_usage: bool = False):
    """Native Anfrage ausfuehren und als OpenAI-Antwort (normal oder SSE) liefern."""
    created = int(time.time())
    cid = _oai_id("chatcmpl" if kind == "chat" else "cmpl")
    if not native["stream"]:
        r = await client.post(path, json=native)
        if r.status_code != 200:
            return _oai_error(r.status_code, r.text)
        d = r.json()
        if kind == "chat":
            m = d.get("message") or {}
            msg = {"role": "assistant", "content": m.get("content", "")}
            if m.get("thinking"):
                msg["reasoning"] = m["thinking"]
                msg["reasoning_content"] = m["thinking"]
            calls = _oai_tool_calls(m.get("tool_calls"))
            if calls:
                msg["tool_calls"] = calls
            choice = {"index": 0, "message": msg, "finish_reason": _oai_finish(d, bool(calls))}
            obj = "chat.completion"
        else:
            choice = {"index": 0, "text": d.get("response", ""), "logprobs": None,
                      "finish_reason": _oai_finish(d, False)}
            obj = "text_completion"
        return JSONResponse({"id": cid, "object": obj, "created": created,
                             "model": client_model, "system_fingerprint": "fp_ollama_proxy",
                             "choices": [choice], "usage": _oai_usage(d)})

    req = client.build_request("POST", path, json=native)
    resp = await client.send(req, stream=True)
    if resp.status_code != 200:
        text = (await resp.aread()).decode(errors="replace")
        await resp.aclose()
        return _oai_error(resp.status_code, text)
    obj = "chat.completion.chunk" if kind == "chat" else "text_completion"

    def sse(choices: list, **extra) -> bytes:
        payload = {"id": cid, "object": obj, "created": created, "model": client_model,
                   "system_fingerprint": "fp_ollama_proxy", "choices": choices, **extra}
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()

    async def gen():
        n_calls, buf = 0, b""
        try:
            if kind == "chat":
                yield sse([{"index": 0, "delta": {"role": "assistant", "content": ""},
                            "finish_reason": None}])
            async for chunk in resp.aiter_raw():
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    if d.get("error"):
                        yield f"data: {json.dumps({'error': {'message': d['error']}})}\n\n".encode()
                        continue
                    if kind == "chat":
                        m = d.get("message") or {}
                        delta = {}
                        if m.get("content"):
                            delta["content"] = m["content"]
                        if m.get("thinking"):
                            delta["reasoning"] = m["thinking"]
                            delta["reasoning_content"] = m["thinking"]
                        calls = _oai_tool_calls(m.get("tool_calls"), start=n_calls)
                        if calls:
                            delta["tool_calls"] = calls
                            n_calls += len(calls)
                        if delta:
                            yield sse([{"index": 0, "delta": delta, "finish_reason": None}])
                    elif d.get("response"):
                        yield sse([{"index": 0, "text": d["response"], "logprobs": None,
                                    "finish_reason": None}])
                    if d.get("done"):
                        fin = _oai_finish(d, n_calls > 0)
                        if kind == "chat":
                            yield sse([{"index": 0, "delta": {}, "finish_reason": fin}])
                        else:
                            yield sse([{"index": 0, "text": "", "logprobs": None,
                                        "finish_reason": fin}])
                        if include_usage:
                            yield sse([], usage=_oai_usage(d))
            yield b"data: [DONE]\n\n"
        finally:
            await resp.aclose()

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})

# ---------------------------------------------------------------------------
# FastAPI
# ---------------------------------------------------------------------------

# Alle KEEP_LOADED_INTERVAL Sekunden pruefen, ob dauerhafte Modelle (warm + keep_alive -1)
# geladen sind -- auch direkt nach dem Start, z.B. nach einem Neustart des Rechners.
# 0 schaltet den Waechter ab.
KEEP_LOADED_INTERVAL = int(os.environ.get("PROXY_KEEP_LOADED_INTERVAL", "60"))


@asynccontextmanager
async def lifespan(_app):
    task = asyncio.create_task(keep_loaded_loop()) if KEEP_LOADED_INTERVAL > 0 else None
    yield
    if task:
        task.cancel()


app = FastAPI(title="OllamaProxy", lifespan=lifespan)

# CORS: erlaubt Web-Clients (rin.chat, andere Browser-basierte UIs), den Proxy
# direkt aus dem Browser anzusprechen. "*" ist hier ok, weil der Proxy im
# lokalen Netz laeuft und Ollama selbst keine Auth prueft. Preflight-OPTIONS
# werden damit automatisch beantwortet.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def ui_lang(request: Request) -> str:
    """Sprache der Oberflaeche (Kopfzeile von /i18n.js, sonst Accept-Language; Standard Deutsch)."""
    return i18n.request_lang(request.headers, "x-ollamaproxy-lang")


def fail(status: int, message: str, request: Request) -> JSONResponse:
    """Fehler der Verwaltungs-Endpunkte; Meldung in der Sprache der Anfrage."""
    return JSONResponse({"error": i18n.translate(message, ui_lang(request))}, status_code=status)


# ---- Native Ollama-Endpunkte ----

_vision_cache: dict[tuple[str, str], str] = {}   # (helper_model, sha256(media)) -> Beschreibung
_VISION_CACHE_MAX = 128
_VISION_PROMPT = (
    "Beschreibe dieses Bild praezise, strukturiert und vollstaendig, damit ein reines "
    "Text-/Coding-Sprachmodell alle relevanten Details kennt. Gib sichtbaren Text, Code, "
    "Fehlermeldungen, Zahlen, Pfade und UI-Elemente exakt im Wortlaut wieder."
)
_AUDIO_PROMPT = (
    "Analysiere diese Audioaufnahme fuer ein nachgeschaltetes Sprachmodell: "
    "Wenn gesprochene Sprache zu hoeren ist, transkribiere sie wortgetreu und vollstaendig "
    "(inkl. Sprecherwechsel oder Tonfall, falls relevant). Wenn Geraeusche, Musik oder "
    "Hintergrundklaenge zu hoeren sind, beschreibe sie praezise."
)

_AUDIO_MAGICS = (
    b"RIFF",            # WAV
    b"ID3",             # MP3 (ID3v2)
    b"\xff\xfb", b"\xff\xf3", b"\xff\xf2", b"\xff\xe3",  # MP3 FrameSync
    b"OggS",            # OGG / Opus / Vorbis
    b"fLaC",            # FLAC
    b"\x1aE\xdf\xa3",   # WebM / Matroska
)


def _is_audio_b64(b64: str) -> bool:
    """Erkennt anhand der ersten Bytes (Magic-Header), ob ein Base64-Block Audio statt Bild ist."""
    try:
        s = b64.strip()
        if s.startswith("data:"):
            head, _, s = s.partition(",")
            if "audio/" in head.lower():
                return True
        raw = base64.b64decode(s[:64] + "===", validate=False)
        if raw.startswith(_AUDIO_MAGICS):
            return True
        if len(raw) >= 12 and raw[4:8] == b"ftyp" and raw[8:12] in (b"M4A ", b"mp42", b"isom", b"3gp4"):
            return True
    except Exception:
        pass
    return False


def _normalize_media_fields(obj: dict) -> list[str]:
    """Sammelt images + evtl. audio/audios-Felder auf einer Nachricht oder dem Request-Body."""
    items = list(obj.get("images") or [])
    for akey in ("audio", "audios"):
        val = obj.pop(akey, None)
        if val:
            alist = [val] if isinstance(val, str) else list(val)
            for a in alist:
                if isinstance(a, str) and a:
                    items.append(a.split(",", 1)[1] if a.startswith("data:") and "," in a else a)
    if items:
        obj["images"] = items
    return items


def _body_has_media(body: dict) -> bool:
    if _normalize_media_fields(body):
        return True
    has = False
    for m in body.get("messages") or []:
        if isinstance(m, dict) and _normalize_media_fields(m):
            has = True
    return has


async def _model_capabilities(model_name: str) -> list[str]:
    try:
        meta = await model_meta(model_name)
        return list(meta.get("capabilities") or [])
    except Exception:
        return ["vision", "audio"]      # im Zweifel nichts anfassen


async def _describe_single_media(helper: str, media_b64: str, user_hint: str, is_audio: bool) -> str:
    digest = hashlib.sha256(media_b64.encode("ascii", errors="ignore")).hexdigest()
    key = (helper, digest)
    if key in _vision_cache:
        return _vision_cache[key]
    he = find_entry(helper)
    real_helper = he["model"] if he else helper
    opts = dict(he["forced_options"]) if he else {"num_ctx": 8192}
    keep_alive = he["keep_alive"] if he else "5m"
    prompt = _AUDIO_PROMPT if is_audio else _VISION_PROMPT
    hint = (user_hint or "").strip()
    if hint:
        prompt += f"\n\nKontext/Frage des Nutzers dazu:\n{hint[:1000]}"
    payload = {
        "model": real_helper,
        "messages": [{"role": "user", "content": prompt, "images": [media_b64]}],
        "stream": False,
        "think": False,
        "keep_alive": keep_alive,
        "options": opts,
    }
    r = await client.post("/api/chat", json=payload)
    if r.status_code != 200:
        raise RuntimeError(f"Medien-Helfer {real_helper} HTTP {r.status_code}: {r.text[:200]}")
    desc = ((r.json().get("message") or {}).get("content") or "").strip()
    if desc:
        if len(_vision_cache) >= _VISION_CACHE_MAX:
            _vision_cache.pop(next(iter(_vision_cache)))
        _vision_cache[key] = desc
    return desc or ("(keine Audio-Transkription erhalten)" if is_audio else "(keine Bildbeschreibung erhalten)")


async def apply_vision_helper(body: dict) -> dict:
    """
    Falls die Anfrage Bilder oder Audio enthaelt, das Zielmodell diese Modalitaet
    aber nicht selbst unterstuetzt (laut capabilities aus /api/show: 'vision' bzw.
    'audio'): Medien vorab durch STATE['vision_helper'] (z.B. gemma4:e4b) beschreiben
    bzw. transkribieren lassen und als Text einsetzen. Medien, die das Zielmodell
    selbst verarbeiten kann, bleiben unangetastet im Request.
    """
    helper = (STATE.get("vision_helper") or "").strip()
    if not helper or not _body_has_media(body):
        return body
    target = body.get("model") or ""
    he = find_entry(helper)
    real_helper = he["model"] if he else helper
    if real_helper == target:
        return body
    caps = await _model_capabilities(target)
    can_vision = "vision" in caps
    can_audio = "audio" in caps
    if can_vision and can_audio:
        return body

    async def _process_items(items: list[str], hint: str) -> tuple[list[str], list[str]]:
        keep, notes = [], []
        img_i = aud_i = 0
        for item in items:
            is_aud = _is_audio_b64(item)
            if (is_aud and can_audio) or (not is_aud and can_vision):
                keep.append(item)
                continue
            desc = await _describe_single_media(real_helper, item, hint, is_aud)
            if is_aud:
                aud_i += 1
                notes.append(f"[Automatische Audio-Transkription/Beschreibung (Audio {aud_i}, via {real_helper}):\n{desc}\n]")
            else:
                img_i += 1
                notes.append(f"[Automatische Bildbeschreibung (Bild {img_i}, via {real_helper}):\n{desc}\n]")
        return keep, notes

    try:
        converted = 0
        # 1) /api/chat: messages[].images
        for m in body.get("messages") or []:
            imgs = m.get("images")
            if not imgs:
                continue
            hint = m.get("content") or ""
            keep, notes = await _process_items(imgs, hint)
            if notes:
                converted += len(notes)
                prefix = "\n\n".join(notes)
                m["content"] = f"{prefix}\n\n{hint}".strip() if hint else prefix
            if keep:
                m["images"] = keep
            else:
                m.pop("images", None)
        # 2) /api/generate: top-level images
        if body.get("images"):
            imgs = body.get("images")
            hint = body.get("prompt") or ""
            keep, notes = await _process_items(imgs, hint)
            if notes:
                converted += len(notes)
                prefix = "\n\n".join(notes)
                body["prompt"] = f"{prefix}\n\n{hint}".strip() if hint else prefix
            if keep:
                body["images"] = keep
            else:
                body.pop("images", None)
        if converted:
            print(f"[media-helper] {converted} Medium/Medien fuer {target} via {real_helper} umgesetzt", flush=True)
    except Exception as ex:
        print(f"[media-helper] Fehler ({real_helper} -> {target}): {ex}", flush=True)
    return body


@app.post("/api/chat")
async def api_chat(request: Request):
    body = cap_predict(await apply_vision_helper(force_native(await request.json())), _client(request))
    return await stream_upstream("/api/chat", body)


@app.post("/api/generate")
async def api_generate(request: Request):
    body = cap_predict(await apply_vision_helper(force_native(await request.json())), _client(request))
    return await stream_upstream("/api/generate", body)


@app.post("/api/embeddings")
async def api_embeddings(request: Request):
    # Embeddings nicht auf das Chat-Modell zwingen -> nur durchreichen
    body = await request.json()
    r = await client.post("/api/embeddings", json=body)
    return JSONResponse(r.json(), status_code=r.status_code)


# ---- OpenAI-kompatible Endpunkte ----

@app.post("/v1/chat/completions")
async def openai_chat(request: Request):
    b = await request.json()
    e = resolve(b.get("model"))
    native = cap_predict(await apply_vision_helper(force_native(oai_chat_to_native(b))), _client(request))
    return await _oai_call("/api/chat", native, shown_name(b.get("model"), e), "chat",
                           include_usage=bool((b.get("stream_options") or {}).get("include_usage")))


@app.post("/v1/completions")
async def openai_completions(request: Request):
    b = await request.json()
    e = resolve(b.get("model"))
    native = cap_predict(force_native(oai_completion_to_native(b)), _client(request))
    return await _oai_call("/api/generate", native, shown_name(b.get("model"), e), "completion",
                           include_usage=bool((b.get("stream_options") or {}).get("include_usage")))


@app.post("/v1/embeddings")
async def openai_embeddings(request: Request):
    # Embeddings nicht auf das Chat-Modell zwingen -> unveraendert durchreichen
    r = await client.post("/v1/embeddings", json=await request.json())
    return JSONResponse(r.json(), status_code=r.status_code)


# ---- Discovery-Endpunkte (GET) ----
# Clients sehen nur die freigegebenen Modelle -- keine Scheinauswahl --, jeweils
# unter Alias, falls gesetzt. Das Standardmodell steht vorne.
# /api/version reicht unveraendert durch.

def _ordered_entries() -> list:
    d = default_entry()
    return [d] + [e for e in entries() if e is not d]


def _renamed(upstream: dict | None, e: dict, name: str | None = None) -> dict:
    name = name or client_name(e)
    item = dict(upstream) if upstream else {}     # Upstream-Objekt nicht mutieren
    item["name"] = name
    item["model"] = name
    return item


@app.get("/api/version")
async def api_version():
    r = await client.get("/api/version")
    return JSONResponse(r.json(), status_code=r.status_code)


@app.get("/api/tags")
async def api_tags():
    """Native Modellliste auf die freigegebenen Modelle reduzieren, ggf. mit Alias."""
    r = await client.get("/api/tags")
    upstream = {m.get("name"): m for m in r.json().get("models", [])}
    return JSONResponse(
        {"models": [_renamed(upstream.get(e["model"]), e, n) for e in _ordered_entries()
                    for n in [None] + [q["alias"] for q in e.get("profiles", [])]]},
        status_code=r.status_code)


@app.get("/api/ps")
async def api_ps():
    """Geladene Modelle -- nur freigegebene, unter dem Namen, den Clients kennen."""
    r = await client.get("/api/ps")
    out = []
    for m in r.json().get("models", []):
        e = find_entry(m.get("name"))
        if e and e["model"] == m.get("name"):
            out.append(_renamed(m, e))
    return JSONResponse({"models": out}, status_code=r.status_code)


@app.get("/v1/models")
async def openai_models():
    """OpenAI-Modellliste auf die freigegebenen Modelle reduzieren, ggf. mit Alias."""
    data = []
    for e in _ordered_entries():
        for name in [client_name(e)] + [q["alias"] for q in e.get("profiles", [])]:
            item = {"id": name, "object": "model", "created": 0, "owned_by": "ollama-proxy"}
            ctx = e["forced_options"].get("num_ctx")
            if ctx:
                # nicht im OpenAI-Standard, aber von vielen Clients gelesen
                # (OpenRouter-Stil "context_length", vLLM-Stil "max_model_len")
                item["context_length"] = item["max_model_len"] = int(ctx)
            data.append(item)
    return JSONResponse({"object": "list", "data": data})


@app.post("/api/show")
async def api_show(request: Request):
    # Alias -> echtes Modell; unbekannte Namen -> Standardmodell
    body = await request.json()
    e = resolve(body.get("model") or body.get("name"))
    body["model"] = e["model"]
    body.pop("name", None)
    r = await client.post("/api/show", json=body)
    data = r.json()
    ctx = e["forced_options"].get("num_ctx")
    if r.status_code == 200 and ctx:
        # der Kontext, mit dem das Modell ueber den Proxy laeuft (wie PARAMETER num_ctx)
        params = [ln for ln in (data.get("parameters") or "").splitlines()
                  if not ln.strip().startswith("num_ctx")]
        data["parameters"] = "\n".join(params + [f"num_ctx {int(ctx)}"]).strip()
    return JSONResponse(data, status_code=r.status_code)


# ---------------------------------------------------------------------------
# Steuer-API
# ---------------------------------------------------------------------------

async def warmup(e: dict):
    """Modell mit seinen erzwungenen Settings laden, damit der Runner bereitsteht."""
    try:
        await client.post("/api/generate", json={
            "model": e["model"],
            "prompt": "",
            "keep_alive": e["keep_alive"],
            "options": dict(e["forced_options"]),
        })
    except Exception:
        pass


async def warmup_all(es: list):
    # nacheinander, damit Ollama nicht mehrere grosse Modelle gleichzeitig laedt
    async with _warm_lock:
        for e in es:
            await warmup(e)


# Von Hand entladene Modelle laesst der Waechter in Ruhe, bis sie wieder geladen
# oder die Modellliste neu angewendet wird.
_manual_unloaded: set = set()
_warm_lock = asyncio.Lock()


async def loaded_contexts() -> dict:
    """Von Ollama geladene Modelle -> Kontext, mit dem sie laufen."""
    r = await client.get("/api/ps", timeout=10)
    return {m.get("name"): m.get("context_length") for m in r.json().get("models", [])}


def needs_load(e: dict, loaded: dict) -> bool:
    """Nicht geladen, oder mit einem anderen Kontext als erzwungen."""
    if e["model"] not in loaded:
        return True
    want = e["forced_options"].get("num_ctx")
    return bool(want) and loaded[e["model"]] not in (None, want)


def is_permanent(e: dict) -> bool:
    return e["warm"] and e["keep_alive"] in (-1, "-1")


async def _fits(e: dict) -> bool:
    """Prognose: passt das Modell noch? Ohne RAM-Werte im Zweifel ja."""
    try:
        mem = await system_memory()
        if not mem:
            return True
        meta = await model_meta(e["model"])
        need = estimate_from_meta(meta, e["forced_options"].get("num_ctx", 8192))["total"]
        return need <= (mem.get("model_budget") or mem["available"])
    except Exception:
        return True


async def keep_loaded_once() -> list:
    """Dauerhafte Modelle laden, die fehlen oder mit falschem Kontext laufen."""
    if _warm_lock.locked():
        return []                                   # es wird gerade geladen
    try:
        loaded = await loaded_contexts()
    except Exception:
        return []                                   # Ollama (noch) nicht erreichbar
    todo = [e for e in entries() if is_permanent(e)
            and e["model"] not in _manual_unloaded and needs_load(e, loaded)]
    done = []
    for e in todo:
        # nur laden, wenn es laut Prognose passt -- sonst wuerde Ollama ein anderes
        # Modell rauswerfen und der Waechter es beim naechsten Durchlauf zurueckholen
        if e["model"] in loaded or await _fits(e):
            await warmup_all([e])
            done.append(e["model"])
    return done


async def keep_loaded_loop():
    await asyncio.sleep(5)                          # Ollama Zeit zum Starten geben
    while True:
        done = await keep_loaded_once()
        if done:
            print(f"[keep-loaded] geladen: {', '.join(done)}", flush=True)
        await modules_idle_check()
        await asyncio.sleep(KEEP_LOADED_INTERVAL)


def _config_view(models: list, default_model: str) -> dict:
    return {"models": models, "default_model": default_model,
            "vision_helper": STATE.get("vision_helper", "")}


@app.get("/admin/state")
async def get_state():
    # verfuegbare Modelle vom Upstream mitliefern
    models, sizes = [], {}
    try:
        r = await client.get("/api/tags")
        tags = r.json().get("models", [])
        models = [m["name"] for m in tags]
        sizes = {m["name"]: m.get("size", 0) for m in tags}
    except Exception:
        pass
    return {
        **_config_view(STATE["models"], STATE["default_model"]),
        "available_models": models,
        "model_sizes": sizes,
        "kv_cache_type": KV_CACHE_TYPE,
        "presets": list(STATE["presets"].keys()),
        "version": __version__,
    }


@app.post("/admin/apply")
async def apply_config(request: Request):
    """
    Komplette Liste der freigegebenen Modelle setzen. Vorgeladen werden Eintraege
    mit warm=true, die neu/geaendert sind, nicht geladen sind oder mit falschem
    Kontext laufen.
    """
    data = await request.json()
    models = [normalize_entry(e) for e in data.get("models") or []]
    default_model = (data.get("default_model") or "").strip()
    err = validate_config(models, default_model)
    if err:
        return fail(400, err, request)
    old = {e["model"]: e for e in STATE["models"]}
    try:
        loaded = await loaded_contexts()
    except Exception:
        loaded = {}
    warming = [e for e in models if e["warm"]
               and (old.get(e["model"]) != e or needs_load(e, loaded))]
    cold = [e["model"] for e in models if not e["warm"] and e["model"] not in loaded]
    STATE["models"], STATE["default_model"] = models, default_model
    if "vision_helper" in data:
        STATE["vision_helper"] = str(data.get("vision_helper") or "").strip()
    save_state(STATE)
    _manual_unloaded.clear()                    # neu angewendet = wieder normal behandeln
    asyncio.create_task(warmup_all(warming))
    return {"ok": True, **_config_view(models, default_model),
            "warming": [e["model"] for e in warming], "not_loaded_cold": cold}


@app.get("/admin/presets")
async def list_presets():
    return STATE["presets"]


@app.post("/admin/presets")
async def save_preset(request: Request):
    """Preset = Momentaufnahme der kompletten Modellliste (aus dem Body oder aktuell)."""
    data = await request.json()
    name = (data.get("name") or "").strip()
    if not name:
        return fail(400, "kein Preset-Name", request)
    models = [normalize_entry(e) for e in data.get("models") or STATE["models"]]
    default_model = data.get("default_model") or STATE["default_model"]
    err = validate_config(models, default_model)
    if err:
        return fail(400, err, request)
    STATE["presets"][name] = _config_view(models, default_model)
    save_state(STATE)
    return {"ok": True, "presets": list(STATE["presets"].keys())}


@app.delete("/admin/presets/{name}")
async def delete_preset(name: str):
    STATE["presets"].pop(name, None)
    save_state(STATE)
    return {"ok": True, "presets": list(STATE["presets"].keys())}


# ---- Modellverwaltung ----

@app.post("/admin/pull")
async def admin_pull(request: Request):
    """Modell vom Registry ziehen, Fortschritt streamen."""
    data = await request.json()
    name = (data.get("name") or "").strip()
    if not name:
        return fail(400, "kein Modellname", request)

    async def gen():
        req = client.build_request("POST", "/api/pull",
                                   json={"model": name, "stream": True})
        resp = await client.send(req, stream=True)
        async for chunk in resp.aiter_raw():
            yield chunk
        await resp.aclose()

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@app.post("/admin/delete")
async def admin_delete(request: Request):
    """Modell vom Ollama-Server loeschen."""
    data = await request.json()
    name = (data.get("name") or "").strip()
    if not name:
        return fail(400, "kein Modellname", request)
    # freigegebene Modelle nicht loeschen lassen
    if any(e["model"] == name for e in entries()):
        return fail(409, "freigegebenes Modell kann nicht gelöscht werden", request)
    req = client.build_request("DELETE", "/api/delete", json={"model": name})
    r = await client.send(req)
    return JSONResponse({"ok": r.status_code == 200,
                         "status": r.status_code}, status_code=r.status_code)


# ---------------------------------------------------------------------------
# Speicher: RAM-Anzeige, geladene Modelle, Laden/Entladen, Prognose
# ---------------------------------------------------------------------------
# Laeuft auf derselben Maschine wie Ollama. Zwei Plattformen:
#   macOS : vm_stat, sysctl, footprint, ioreg (Mac mini, Apple Silicon)
#   Linux : /proc/meminfo, /proc/<pid>, amdgpu-sysfs (Strix Halo; auch in Docker,
#           dann mit pid: "service:ollama", damit die Runner sichtbar sind)
# Anderswo liefern die Endpunkte leere Werte statt Fehler.
#
# Warum eigene Messung: Ollamas /api/ps meldet nur, was der Runner selbst anlegt
# (KV-Cache, Puffer). Seit Ollama 0.40 werden die Gewichte direkt aus der Datei in
# den GPU-Speicher eingeblendet; die zaehlt macOS dem Grafiktreiber zu, nicht dem
# Prozess (gemma4:12b: ollama ps 1,4 GB, Prozess 0,02 GB, real ~8,5 GB). Deshalb:
# pro Modell max(Prozessmessung, Prognose), dazu der GPU-Speicher aus ioreg.

MIB = 1024 * 1024
GIB = 1024 * MIB

# Ollama nimmt den KV-Cache-Typ NICHT pro Request an (cache_type_k/v in options
# werden ignoriert), sondern nur aus der Server-Env OLLAMA_KV_CACHE_TYPE.
KV_CACHE_TYPE = os.environ.get("OLLAMA_KV_CACHE_TYPE", "f16")
KV_BYTES_PER_ELEM = {"f16": 2.0, "q8_0": 34 / 32, "q4_0": 18 / 32}

# Zuschlag fuer Rechenpuffer, Prompt-Cache, Metal-Runtime. Kalibriert an
# gemma4:e4b @ 8192 (gemessen 10399 MiB = Datei 9163 + KV 168 + ~1070).
OVERHEAD_BASE = 700 * MIB
OVERHEAD_MULTIMODAL = 300 * MIB     # Bild-/Audio-Encoder (mmproj) samt Puffern
SAFETY_MARGIN = 1 * GIB             # so viel soll nach dem Laden frei bleiben
# llama.cpp legt fuer Sliding-Window-Layer window + n_ubatch Zellen an
UBATCH = 512

IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")
# fuer Tests umbiegbar (Fake-/proc und Fake-/sys)
PROC_ROOT = Path(os.environ.get("PROXY_PROC_ROOT", "/proc"))
SYS_ROOT = Path(os.environ.get("PROXY_SYS_ROOT", "/sys"))

_meta_cache: dict = {}              # name -> (modified_at, meta)


async def _run(*cmd: str, timeout: float = 10) -> str | None:
    """Kommando ausfuehren, stdout liefern; None wenn nicht vorhanden/Fehler."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        return out.decode(errors="replace") if proc.returncode == 0 else None
    except Exception:
        return None


async def system_memory() -> dict | None:
    """RAM/Swap/GPU-Speicher der Maschine, auf der der Proxy laeuft."""
    try:
        if IS_MAC:
            return await _system_memory_macos()
        if IS_LINUX:
            return _system_memory_linux()
    except Exception:
        pass
    return None


async def _system_memory_macos() -> dict | None:
    """RAM/Swap des Macs aus sysctl + vm_stat, GPU-Speicher aus ioreg."""
    total = await _run("sysctl", "-n", "hw.memsize")
    vm = await _run("vm_stat")
    if not total or not vm:
        return None
    m = re.search(r"page size of (\d+) bytes", vm)
    page = int(m.group(1)) if m else 4096
    pages = {k.strip(): int(v.strip().rstrip("."))
             for k, v in re.findall(r"^([^:\"]+):\s+(\d+)\.?$", vm, re.M)}
    get = lambda k: pages.get(k, 0) * page
    total = int(total.strip())
    # sofort nutzbar, ohne dass macOS komprimieren oder swappen muss
    available = get("Pages free") + get("Pages inactive") + get("Pages speculative")
    swap_total = swap_used = 0
    sw = await _run("sysctl", "-n", "vm.swapusage")
    if sw:
        t = re.search(r"total = ([\d.]+)M", sw)
        u = re.search(r"used = ([\d.]+)M", sw)
        swap_total = int(float(t.group(1)) * MIB) if t else 0
        swap_used = int(float(u.group(1)) * MIB) if u else 0
    gpu_in_use = None
    io = await _run("ioreg", "-r", "-c", "IOAccelerator", "-d", "1", "-w0")
    if io:
        g = re.search(r'"In use system memory"=(\d+)', io)
        gpu_in_use = int(g.group(1)) if g else None
    return {
        "total": total,
        "available": available,
        "gpu_in_use": gpu_in_use,
        "used": total - available,
        "wired": get("Pages wired down"),
        "compressed": get("Pages occupied by compressor"),
        "swap_total": swap_total,
        "swap_used": swap_used,
    }


def parse_meminfo(text: str) -> dict:
    """/proc/meminfo -> {Schluessel: Bytes}."""
    out = {}
    for line in text.splitlines():
        m = re.match(r"([\w()]+):\s+(\d+)(\s+kB)?\s*$", line)
        if m:
            out[m.group(1)] = int(m.group(2)) * (1024 if m.group(3) else 1)
    return out


def amdgpu_memory(sys_root: Path = None) -> dict | None:
    """
    VRAM- und GTT-Belegung aller amdgpu-Karten aus sysfs. Beim Strix Halo ist VRAM
    nur der im BIOS reservierte Teil (UMA-Framebuffer); das Gros der Modelle liegt
    im GTT, also in normalem System-RAM, den die GPU mitbenutzt.
    """
    sys_root = sys_root or SYS_ROOT
    tot = {"vram_total": 0, "vram_used": 0, "gtt_total": 0, "gtt_used": 0}
    seen, found = set(), False
    for card in sorted((sys_root / "class" / "drm").glob("card*")):
        if not re.fullmatch(r"card\d+", card.name):
            continue                      # Anschluesse wie card0-DP-1 ueberspringen
        dev = card / "device"
        if not (dev / "mem_info_vram_total").exists():
            continue
        key = os.path.realpath(dev)
        if key in seen:
            continue
        seen.add(key)
        found = True
        for k in tot:
            try:
                tot[k] += int((dev / f"mem_info_{k}").read_text().strip())
            except Exception:
                pass
    return tot if found else None


def _system_memory_linux() -> dict | None:
    mi = parse_meminfo((PROC_ROOT / "meminfo").read_text())
    total, available = mi.get("MemTotal"), mi.get("MemAvailable")
    if not total or available is None:
        return None
    swap_total = mi.get("SwapTotal", 0)
    gpu = amdgpu_memory()
    res = {
        "total": total,
        "available": available,
        "used": total - available,
        "wired": None,
        "compressed": mi.get("Zswap"),          # nur mit aktivem zswap vorhanden
        "swap_total": swap_total,
        "swap_used": swap_total - mi.get("SwapFree", 0),
        "gpu_in_use": (gpu["vram_used"] + gpu["gtt_used"]) if gpu else None,
        "gpu": gpu,
    }
    if gpu:
        # Was ein neues Modell bekommen kann: freies VRAM plus GTT, wobei GTT aus dem
        # System-RAM kommt und deshalb auch durch MemAvailable begrenzt ist.
        vram_free = max(gpu["vram_total"] - gpu["vram_used"], 0)
        gtt_free = max(gpu["gtt_total"] - gpu["gtt_used"], 0)
        res["model_budget"] = vram_free + min(available, gtt_free)
    return res


async def model_meta(name: str) -> dict:
    """Dateigroesse, Faehigkeiten, Architektur und Blob-Pfad eines Modells (gecacht)."""
    tags = (await client.get("/api/tags")).json().get("models", [])
    tag = next((m for m in tags if m.get("name") == name), None)
    if tag is None:
        raise KeyError(name)
    cached = _meta_cache.get(name)
    if cached and cached[0] == tag.get("modified_at"):
        return cached[1]
    show = (await client.post("/api/show", json={"model": name})).json()
    info = show.get("model_info") or {}
    # Per-Layer-Arrays (z.B. sliding_window_pattern) liefert Ollama nur mit
    # verbose; die Antwort ist gross (Tensorliste), daher nur bei Bedarf.
    if any(v is None for k, v in info.items() if ".attention." in k):
        verbose = (await client.post("/api/show",
                                     json={"model": name, "verbose": True})).json()
        info = verbose.get("model_info") or info
    blob = re.search(r"^FROM\s+(\S+)", show.get("modelfile", ""), re.M)
    meta = {
        "name": name,
        "file_size": tag.get("size", 0),
        "capabilities": show.get("capabilities") or [],
        "model_info": {k: v for k, v in info.items()
                       if k.startswith("general.architecture") or ".attention." in k
                       or k.endswith((".block_count", ".embedding_length",
                                      ".context_length"))},
        "blob": blob.group(1) if blob else None,
    }
    _meta_cache[name] = (tag.get("modified_at"), meta)
    return meta


def kv_cache_bytes(info: dict, num_ctx: int, cache_type: str = KV_CACHE_TYPE) -> int | None:
    """
    KV-Cache-Groesse wie llama.cpp sie anlegt: pro Layer mit eigenem KV
    Zellen x KV-Koepfe x (K-Dim + V-Dim) x Bytes. Sliding-Window-Layer brauchen
    nur window + ubatch Zellen, geteilte KV-Layer (Gemma 3n/4) gar keine.
    Unbekannte Architekturdetails -> konservativ als voller Kontext.
    """
    arch = info.get("general.architecture")
    g = lambda k: info.get(f"{arch}.{k}")
    n_layer = g("block_count")
    head_count = g("attention.head_count")
    if not arch or not n_layer or not head_count:
        return None
    at = lambda v, i: v[i] if isinstance(v, list) else v
    hkv = g("attention.head_count_kv") or head_count
    emb = g("embedding_length") or 0
    k_len = g("attention.key_length") or (emb // at(head_count, 0) if emb else 0)
    v_len = g("attention.value_length") or k_len
    k_swa = g("attention.key_length_swa") or k_len
    v_swa = g("attention.value_length_swa") or v_len
    window = g("attention.sliding_window")
    pattern = g("attention.sliding_window_pattern")
    shared = g("attention.shared_kv_layers") or 0
    if not k_len:
        return None
    bpe = KV_BYTES_PER_ELEM.get(cache_type, 2.0)
    total = 0.0
    for i in range(n_layer - shared):
        swa = bool(window) and isinstance(pattern, list) and i < len(pattern) and pattern[i]
        cells = min(num_ctx, window + UBATCH) if swa else num_ctx
        kd, vd = (k_swa, v_swa) if swa else (k_len, v_len)
        total += cells * (at(hkv, i) or 0) * (kd + vd) * bpe
    return int(total)


def estimate_from_meta(meta: dict, num_ctx: int) -> dict:
    kv = kv_cache_bytes(meta["model_info"], num_ctx)
    multimodal = any(c in meta["capabilities"] for c in ("vision", "audio"))
    overhead = OVERHEAD_BASE + (OVERHEAD_MULTIMODAL if multimodal else 0)
    # ohne Architekturdaten: KV grob mit 10 % der Gewichte ansetzen
    kv_est = kv if kv is not None else int(meta["file_size"] * 0.10)
    return {
        "weights": meta["file_size"],
        "kv_cache": kv_est,
        "kv_exact": kv is not None,
        "kv_cache_type": KV_CACHE_TYPE,
        "overhead": overhead,
        "total": meta["file_size"] + kv_est + overhead,
    }


async def runner_footprints() -> dict:
    """Blob-Pfad -> {pid, footprint} fuer alle laufenden Ollama-Runner."""
    try:
        if IS_MAC:
            return await _runner_footprints_macos()
        if IS_LINUX:
            return _runner_footprints_linux()
    except Exception:
        pass
    return {}


def _runner_footprints_linux(proc_root: Path = None) -> dict:
    """
    Runner aus /proc: llama-server bzw. "ollama runner" mit --model <blob>.
    Gemessen wird RSS; was im GPU-Speicher (VRAM/GTT) liegt, fehlt darin — dafuer
    sorgt spaeter max(Messung, Prognose).
    """
    proc_root = proc_root or PROC_ROOT
    out = {}
    for d in proc_root.iterdir():
        if not d.name.isdigit():
            continue
        try:
            args = [a.decode(errors="replace")
                    for a in (d / "cmdline").read_bytes().split(b"\0") if a]
        except Exception:
            continue
        if "--model" not in args or args.index("--model") + 1 >= len(args):
            continue
        exe = os.path.basename(args[0])
        if exe != "llama-server" and "runner" not in args[1:3]:
            continue
        rss = None
        try:
            m = re.search(r"^VmRSS:\s+(\d+)\s+kB", (d / "status").read_text(), re.M)
            rss = int(m.group(1)) * 1024 if m else None
        except Exception:
            pass
        out[args[args.index("--model") + 1]] = {"pid": int(d.name), "footprint": rss}
    return out


async def _runner_footprints_macos() -> dict:
    """
    macOS: Blob-Pfad -> {pid, footprint} fuer alle laufenden Ollama-Runner.

    Gemessen wird max(footprint, RSS): Runner mit --no-mmap (z.B. gemma4) halten
    die Gewichte in eigenem Speicher, den nur footprint voll zaehlt (RSS zeigt
    dort weniger). Runner mit mmap lesen die Gewichte aus der Datei; diese Seiten
    zaehlt footprint nicht mit, RSS schon.
    """
    ps = await _run("ps", "-axo", "pid=,rss=,command=")
    if not ps:
        return {}
    runners, rss = {}, {}
    for line in ps.splitlines():
        if "llama-server" not in line and " runner " not in line:
            continue
        m = re.match(r"\s*(\d+)\s+(\d+)\s+.*?--model\s+(\S+)", line)
        if m:
            pid = int(m.group(1))
            runners[pid] = m.group(3)
            rss[pid] = int(m.group(2)) * 1024
    if not runners:
        return {}
    fp = {}
    with tempfile.NamedTemporaryFile(suffix=".json") as tmp:
        if await _run("footprint", "-j", tmp.name, *map(str, runners), timeout=20) is not None:
            try:
                data = json.loads(Path(tmp.name).read_text())
                fp = {p["pid"]: p["footprint"] for p in data.get("processes", [])}
            except Exception:
                pass
    return {blob: {"pid": pid, "footprint": max(fp.get(pid) or 0, rss[pid]) or None}
            for pid, blob in runners.items()}


async def loaded_models(mem: dict | None = None, mods: list | None = None) -> list:
    """Von Ollama geladene Modelle, ergaenzt um den gemessenen Speicher."""
    ps = (await client.get("/api/ps")).json().get("models", [])
    runners = await runner_footprints()
    # Unter Linux mit amdgpu-GTT liegt der GPU-Teil im System-RAM, wird aber beim
    # Grafiktreiber gezaehlt (nicht im RSS des Runners). Gesamter Verbrauch eines
    # Modells ist daher Prozess-RSS + sein Anteil am GPU-Speicher (abzgl. der Module, z.B. ComfyUI).
    gpu = (mem or {}).get("gpu")
    mods_gpu = sum(m.get("gpu") or 0 for m in (mods or []))
    ollama_gpu_pool = max((gpu["vram_used"] + gpu["gtt_used"]) - mods_gpu, 0) if gpu else 0
    total_vram_rep = sum((m.get("size_vram") or m.get("size") or 0) for m in ps)
    out = []
    for m in ps:
        name = m.get("name")
        vram_rep = m.get("size_vram") or m.get("size") or 0
        gpu_share = int(ollama_gpu_pool * (vram_rep / total_vram_rep)) if (gpu and total_vram_rep > 0) else None
        entry = {
            "name": name,
            "context_length": m.get("context_length"),
            "ollama_size": m.get("size"),
            "expires_at": m.get("expires_at"),
            "promoted": find_entry(name) is not None and find_entry(name)["model"] == name,
            "is_default": name == STATE["default_model"],
            "footprint": None,      # Prozessmessung: RSS (Linux) bzw. max(footprint, RSS) (macOS)
            "gpu_bytes": gpu_share, # Anteil am amdgpu-Speicher (GTT+VRAM), nur Linux
            "estimate": None,       # Prognose fuer den geladenen Kontext
            "used": None,           # angezeigter Wert
        }
        try:
            meta = await model_meta(name)
            run = runners.get(meta["blob"]) if meta["blob"] else None
            if run:
                entry["footprint"] = run["footprint"]
            if m.get("context_length"):
                entry["estimate"] = estimate_from_meta(meta, m["context_length"])["total"]
        except Exception:
            pass
        # Linux mit amdgpu: Prozess-RAM (CPU-Puffer/PLE) + GPU-Anteil (GTT+VRAM) addieren,
        # weil GTT-Seiten nicht im Prozess-RSS auftauchen, aber trotzdem System-RAM belegen.
        measured = ((entry["footprint"] or 0) + gpu_share) if gpu_share is not None else entry["footprint"]
        vals = [v for v in (measured, entry["estimate"]) if v]
        entry["used"] = max(vals) if vals else m.get("size")
        out.append(entry)
    return out


# --------------------------------------------------------------------------- Module
# Andere Dienste auf derselben Maschine (ImageGen mit ComfyUI, FaceRec, ...) koennen sich als
# Modul einklinken. Sie stehen in modules.json (PROXY_MODULES_FILE, Standard neben dem State) als Liste
# {"name", "url", optional "public_url", "idle_free_minutes", "token"} und bieten selbst an:
#   GET  <url>/proxy-module          {name, version, links:[{label, path|port|url}], capabilities, idle_free_minutes}
#   GET  <url>/proxy-module/memory   {rss, gpu, held, reclaimable, busy, detail}   (Faehigkeit "memory")
#   POST <url>/proxy-module/free     Speicher freigeben                            (Faehigkeit "free")
# Die Speicheranzeige zeigt je Modul Links und Verbrauch, rechnet "held" zu den Modellen und zieht "gpu" vom
# GPU-Anteil der Sprachmodelle ab. Der Waechter gibt Module mit "free" nach idle_free_minutes Leerlauf frei.
MODULES_FILE = Path(os.environ.get("PROXY_MODULES_FILE") or (STATE_FILE.parent / "modules.json"))
MODULE_INFO_TTL = 60                # Sekunden, so lange gilt die Selbstauskunft eines Moduls
_module_info: dict[tuple[str, str], tuple[float, dict]] = {}   # (url, Sprache) -> (Zeit, Selbstauskunft)
_module_idle_since: dict[str, float] = {}


def _legacy_modules() -> list[dict]:
    """Fruehere Einstellung per Umgebung (COMFYUI_URL, WEBAPP_/FACEREC_PUBLIC_PORT) -> Module."""
    comfy = os.environ.get("COMFYUI_URL", "").rstrip("/")
    if not comfy:
        return []
    parts = urlsplit(comfy)
    out = []
    for name, var, default in (("ImageGen", "WEBAPP_PUBLIC_PORT", "8190"),
                               ("FaceRec", "FACEREC_PUBLIC_PORT", "8191")):
        port = int(os.environ.get(var, default) or 0)
        if port:
            m = {"name": name, "url": f"{parts.scheme}://{parts.hostname}:{port}"}
            if name == "ImageGen" and os.environ.get("COMFYUI_IDLE_FREE_MINUTES"):
                m["idle_free_minutes"] = float(os.environ["COMFYUI_IDLE_FREE_MINUTES"])
            out.append(m)
    return out


def load_modules() -> list[dict]:
    if MODULES_FILE.exists():
        try:
            data = json.loads(MODULES_FILE.read_text(encoding="utf-8"))
            mods = data.get("modules", data) if isinstance(data, dict) else data
            return [dict(m, url=str(m["url"]).rstrip("/")) for m in mods if isinstance(m, dict) and m.get("url")]
        except (ValueError, KeyError, OSError) as e:
            print(f"[module] {MODULES_FILE} nicht lesbar: {e}", flush=True)
            return []
    return _legacy_modules()


MODULES = load_modules()


def _module_headers(m: dict) -> dict:
    return {"Authorization": f"Bearer {m['token']}"} if m.get("token") else {}


async def module_status(m: dict, lang: str = "de") -> dict:
    """Selbstauskunft + Speicher eines Moduls; nicht erreichbar -> reachable False.
    Die Sprache der Texte (Link-Beschriftung, detail) bestimmt das Modul anhand von Accept-Language."""
    out = {"name": m.get("name") or m["url"], "url": m["url"], "public_url": m.get("public_url"),
           "port": urlsplit(m["url"]).port, "reachable": False, "links": [], "capabilities": [],
           "held": 0, "gpu": 0, "rss": None, "reclaimable": False, "busy": False, "detail": None}
    now = time.time()
    try:
        async with httpx.AsyncClient(timeout=3, headers={**_module_headers(m), "Accept-Language": lang}) as c:
            cached = _module_info.get((m["url"], lang))
            if cached and now - cached[0] < MODULE_INFO_TTL:
                info = cached[1]
            else:
                info = (await c.get(f"{m['url']}/proxy-module")).raise_for_status().json()
                _module_info[(m["url"], lang)] = (now, info)
            out.update(reachable=True, version=info.get("version"), links=info.get("links") or [],
                       capabilities=info.get("capabilities") or [])
            if not m.get("name") and info.get("name"):
                out["name"] = info["name"]
            out["idle_free_minutes"] = float(m.get("idle_free_minutes", info.get("idle_free_minutes") or 0))
            if "memory" in out["capabilities"]:
                mem = (await c.get(f"{m['url']}/proxy-module/memory")).raise_for_status().json()
                for k in ("rss", "gpu", "held", "reclaimable", "busy", "detail"):
                    if k in mem:
                        out[k] = mem[k]
                out["gpu"] = out["gpu"] or 0
                out["held"] = out["held"] or 0
    except (httpx.HTTPError, ValueError) as e:
        _module_info.pop((m["url"], lang), None)
        out["error"] = type(e).__name__
    return out


async def modules_status(lang: str = "de") -> list[dict]:
    return list(await asyncio.gather(*(module_status(m, lang) for m in MODULES))) if MODULES else []


def find_module(name: str) -> dict | None:
    """Modul nach Namen aus modules.json, sonst nach dem Namen aus seiner Selbstauskunft oder der Adresse."""
    for m in MODULES:
        names = {i.get("name") for (url, _), (_, i) in list(_module_info.items()) if url == m["url"]}
        if name in {m.get("name"), m["url"], *names}:
            return m
    return None


async def module_free(m: dict) -> int:
    async with httpx.AsyncClient(timeout=65, headers=_module_headers(m)) as c:
        return (await c.post(f"{m['url']}/proxy-module/free")).status_code


async def modules_idle_check():
    """Waechter-Teil: Module mit Faehigkeit "free", die Speicher halten und seit idle_free_minutes nichts zu
    tun haben, werden freigegeben -- damit Ollama wieder Platz hat."""
    now = asyncio.get_event_loop().time()
    for st in await modules_status():
        key = st["url"]
        minutes = st.get("idle_free_minutes") or 0
        if (not st["reachable"] or "free" not in st["capabilities"] or minutes <= 0
                or st["busy"] or not st["reclaimable"]):
            _module_idle_since.pop(key, None)
            continue
        since = _module_idle_since.setdefault(key, now)
        if now - since >= minutes * 60:
            m = next(x for x in MODULES if x["url"] == key)
            try:
                if await module_free(m) == 200:
                    print(f"[keep-loaded] Modul {st['name']} nach {minutes:g} Min. Leerlauf freigegeben "
                          f"(vorher {st['held'] / GIB:.1f} GB)", flush=True)
            except httpx.HTTPError:
                pass
            _module_idle_since.pop(key, None)


@app.get("/admin/modules")
async def admin_modules(request: Request):
    return await modules_status(ui_lang(request))


@app.post("/admin/modules/free")
async def admin_module_free(request: Request):
    """Ein Modul soll seinen Speicher freigeben ({"name": ...})."""
    name = (await request.json()).get("name", "")
    m = find_module(name)
    if not m:
        return fail(404, "Modul unbekannt", request)
    try:
        code = await module_free(m)
        return JSONResponse({"ok": code == 200}, status_code=code)
    except httpx.HTTPError as e:
        return fail(502, f"Modul nicht erreichbar: {type(e).__name__}", request)


@app.get("/admin/memory")
async def admin_memory(request: Request):
    """RAM-Zustand + geladene Modelle fuer die UI."""
    mem = await system_memory()
    mods = await modules_status(ui_lang(request))
    try:
        loaded = await loaded_models(mem, mods)
    except Exception:
        loaded = []
    models_bytes = sum(m["used"] or 0 for m in loaded) + sum(m["held"] or 0 for m in mods)
    return {"memory": mem, "loaded": loaded, "models_bytes": models_bytes,
            "kv_cache_type": KV_CACHE_TYPE, "modules": mods}


@app.post("/admin/estimate")
async def admin_estimate(request: Request):
    """Prognose: passt Modell X mit num_ctx N noch in den RAM?"""
    data = await request.json()
    name = (data.get("model") or "").strip()
    e = find_entry(name)
    num_ctx = int(data.get("num_ctx") or default_entry()["forced_options"].get("num_ctx", 8192))
    if e:
        # freigegebenes Modell (auch per Alias): immer mit seinen erzwungenen Settings
        name = e["model"]
        num_ctx = e["forced_options"].get("num_ctx", num_ctx)
    try:
        meta = await model_meta(name)
    except KeyError:
        return fail(404, "Modell nicht installiert", request)
    est = estimate_from_meta(meta, num_ctx)
    mem = await system_memory()
    loaded = await loaded_models(mem, await modules_status())
    already = next((m for m in loaded if m["name"] == name), None)
    result = {"model": name, "num_ctx": num_ctx, "estimate": est,
              "memory": mem, "already_loaded": bool(already)}
    if not mem:
        result["verdict"] = "unknown"
        return result
    # Linux/amdgpu: was GPU + RAM zusammen noch hergeben; macOS: freier RAM
    avail = mem.get("model_budget") or mem["available"]
    # Ist das Modell schon geladen, wird es ersetzt: sein Speicher wird frei
    if already and already["used"]:
        avail += already["used"]
    need = est["total"]
    if need <= avail - SAFETY_MARGIN:
        verdict = "fits"
    elif need <= avail + SAFETY_MARGIN:
        verdict = "tight"
    else:
        verdict = "no"
    result["verdict"] = verdict
    result["free_after"] = avail - need
    # Welche anderen geladenen Modelle muesste man entladen, damit es passt?
    result["fits_after_unload"] = [
        m["name"] for m in loaded
        if m["name"] != name and m["used"]
        and need <= avail + m["used"] - SAFETY_MARGIN
    ] if verdict != "fits" else []
    return result


@app.post("/admin/load")
async def admin_load(request: Request):
    """Zusaetzliches Modell laden (bleibt laut keep_alive im Speicher)."""
    data = await request.json()
    name = (data.get("model") or "").strip()
    if not name:
        return fail(400, "kein Modellname", request)
    num_ctx = int(data.get("num_ctx") or default_entry()["forced_options"].get("num_ctx", 8192))
    keep_alive = data.get("keep_alive", -1)
    e = find_entry(name)
    if e:
        # freigegebenes Modell (auch per Alias) immer mit seinen Settings,
        # sonst Reload beim naechsten Request
        name = e["model"]
        num_ctx = e["forced_options"].get("num_ctx", num_ctx)
        keep_alive = e["keep_alive"]
    _manual_unloaded.discard(name)
    async with _warm_lock:
        r = await client.post("/api/generate", json={
            "model": name, "prompt": "", "keep_alive": keep_alive,
            "options": {"num_ctx": num_ctx}})
    if r.status_code != 200:
        try:
            err = r.json().get("error", r.text)
        except Exception:
            err = r.text
        return JSONResponse({"error": err}, status_code=r.status_code)
    return {"ok": True, "model": name, "num_ctx": num_ctx}


@app.post("/admin/unload")
async def admin_unload(request: Request):
    """Modell aus dem Speicher werfen (keep_alive 0). Bleibt installiert."""
    data = await request.json()
    name = (data.get("model") or "").strip()
    e = find_entry(name)
    if e:
        name = e["model"]                      # Alias -> echter Name
    if not name:
        return fail(400, "kein Modellname", request)
    _manual_unloaded.add(name)                 # Waechter laedt es nicht zurueck
    r = await client.post("/api/generate", json={"model": name, "keep_alive": 0})
    return JSONResponse({"ok": r.status_code == 200}, status_code=r.status_code)


# ---------------------------------------------------------------------------
# Web-UI
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def ui():
    return HTML_UI


@app.get("/i18n.js")
async def i18n_js():
    return Response(i18n.script("ollamaproxy.lang", "X-OllamaProxy-Lang"), media_type="text/javascript; charset=utf-8")


HTML_UI = """<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OllamaProxy</title>
<script src="/i18n.js"></script>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin:0; font:15px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;
         background:#0d0f12; color:#d7dce2; }
  .wrap { max-width:680px; margin:0 auto; padding:28px 20px 60px; }
  h1 { font-size:18px; letter-spacing:.04em; font-weight:600; margin:0 0 4px; }
  .sub { color:#6b7280; font-size:12px; margin-bottom:26px; }
  .card { background:#15181d; border:1px solid #232830; border-radius:10px;
          padding:18px 18px 20px; margin-bottom:18px; }
  label { display:block; font-size:11px; text-transform:uppercase;
          letter-spacing:.06em; color:#8b94a3; margin:14px 0 5px; }
  label:first-child { margin-top:0; }
  select,input { width:100%; background:#0d0f12; border:1px solid #2a313b;
          color:#e8edf3; border-radius:7px; padding:9px 10px; font:inherit; }
  select:focus,input:focus { outline:none; border-color:#4b87ff; }
  .row { display:grid; grid-template-columns:1fr 1fr; gap:12px; }
  .row3 { display:grid; grid-template-columns:1fr 1fr 1fr; gap:12px; }
  button { border:none; border-radius:7px; padding:10px 16px; font:inherit;
           font-weight:600; cursor:pointer; }
  .primary { background:#4b87ff; color:#fff; width:100%; margin-top:18px; }
  .primary:hover { background:#3d75e8; }
  .ghost { background:#232830; color:#cfd6df; }
  .ghost:hover { background:#2c333d; }
  .presets { display:flex; flex-wrap:wrap; gap:8px; margin-top:6px; }
  .chip { display:inline-flex; align-items:center; gap:8px; background:#1b2027;
          border:1px solid #2a313b; border-radius:20px; padding:5px 6px 5px 13px;
          font-size:13px; }
  .chip button { background:none; color:#6b7280; padding:2px 6px; font-size:14px; }
  .chip button:hover { color:#ff6b6b; }
  .chip .load { color:#9db4ff; cursor:pointer; }
  .status { font-size:12px; margin-top:12px; min-height:16px; color:#6dd58c; }
  .saverow { display:flex; gap:8px; margin-top:6px; }
  .saverow input { flex:1; }
  .note { font-size:11px; color:#6b7280; margin-top:14px; line-height:1.4; }
  .pullbar { height:6px; background:#1b2027; border-radius:3px; margin-top:12px;
             overflow:hidden; display:none; }
  .pullbar.active { display:block; }
  .pullfill { height:100%; width:0%; background:#4b87ff; transition:width .3s; }
  .modellist { display:flex; flex-direction:column; gap:6px; margin-top:6px; }
  .modelrow { display:flex; align-items:center; justify-content:space-between;
              background:#1b2027; border:1px solid #2a313b; border-radius:7px;
              padding:8px 10px 8px 13px; font-size:13px; gap:10px; }
  .modelrow .mname { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .modelrow .active-tag { color:#6dd58c; font-size:11px; flex-shrink:0; }
  .modelrow button { background:none; color:#6b7280; padding:2px 8px; font-size:14px;
                     border:none; cursor:pointer; flex-shrink:0; }
  .modelrow button:hover { color:#ff6b6b; }
  .membar { display:flex; height:14px; border-radius:4px; overflow:hidden;
            background:#1b2027; margin:8px 0 8px; }
  .membar div { height:100%; transition:width .4s; }
  .seg-models { background:#4b87ff; }
  .seg-other { background:#5b6472; }
  .seg-est { background:repeating-linear-gradient(45deg,#6dd58c 0 5px,#3f8a55 5px 10px); }
  .seg-est.tight { background:repeating-linear-gradient(45deg,#e0b341 0 5px,#9c7a26 5px 10px); }
  .seg-est.no { background:repeating-linear-gradient(45deg,#ff6b6b 0 5px,#a33 5px 10px); }
  .legend { display:flex; flex-wrap:wrap; gap:4px 14px; font-size:11px; color:#8b94a3; }
  .legend i { display:inline-block; width:9px; height:9px; border-radius:2px;
              margin-right:5px; vertical-align:-1px; }
  .memstats { display:grid; grid-template-columns:repeat(4,1fr); gap:8px; margin-top:12px; }
  .memstats div { background:#1b2027; border:1px solid #2a313b; border-radius:7px;
                  padding:7px 9px; }
  .memstats b { display:block; font-size:15px; color:#e8edf3; }
  .memstats span { font-size:10px; text-transform:uppercase; letter-spacing:.05em;
                   color:#6b7280; }
  .warn { color:#e0b341; }
  .bad { color:#ff6b6b; }
  .good { color:#6dd58c; }
  .loadedrow { background:#1b2027; border:1px solid #2a313b; border-radius:7px;
               padding:8px 10px 8px 13px; font-size:13px; display:flex;
               align-items:center; gap:10px; }
  .loadedrow .info { flex:1; min-width:0; }
  .loadedrow .mname { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .loadedrow .meta { font-size:11px; color:#6b7280; }
  .loadedrow .active-tag { color:#6dd58c; font-size:11px; }
  .estimate { background:#1b2027; border:1px solid #2a313b; border-radius:7px;
              padding:10px 12px; margin-top:12px; font-size:13px; display:none; }
  .estimate.show { display:block; }
  .estimate .verdict { font-weight:600; margin-bottom:4px; }
  .estimate .parts { font-size:11px; color:#8b94a3; }
  .entry { background:#11141a; border:1px solid #2a313b; border-radius:8px;
           padding:10px 12px 12px; margin-top:10px; }
  .entry.is-default { border-color:#3d5fa8; }
  .entry-head { display:flex; align-items:center; gap:8px; }
  .entry-head select { flex:1; min-width:0; }
  .entry-head .star { background:none; border:1px solid #2a313b; color:#6b7280;
                      padding:7px 10px; font-size:14px; }
  .entry.is-default .star { color:#ffd166; border-color:#3d5fa8; }
  .entry-head .del { background:none; color:#6b7280; padding:7px 9px; font-size:14px; }
  .entry-head .del:hover { color:#ff6b6b; }
  .entry label { margin-top:10px; }
  .entry .check { display:flex; align-items:center; gap:7px; font-size:12px;
                  color:#8b94a3; margin-top:28px; text-transform:none; letter-spacing:0; }
  .entry .check input { width:auto; }
  .tag { font-size:11px; flex-shrink:0; }
  .tag.promoted { color:#9db4ff; }
  .tag.def { color:#ffd166; }
  @media (max-width:520px) {
    .memstats { grid-template-columns:1fr 1fr; }
    .row3 { grid-template-columns:1fr; }
  }
  textarea { width:100%; background:#0d0f12; border:1px solid #2a313b; color:#e8edf3;
             border-radius:7px; padding:9px 10px; font:inherit; resize:vertical; }
  textarea:focus { outline:none; border-color:#4b87ff; }
  .chatlog { background:#0d0f12; border:1px solid #2a313b; border-radius:7px;
             height:280px; overflow-y:auto; padding:10px 12px; font-size:13px; margin-top:10px;
             resize:vertical; }
  .chatlog .empty { color:#6b7280; font-size:12px; }
  .msg { margin-bottom:12px; }
  .msg .who { font-size:10px; text-transform:uppercase; letter-spacing:.06em;
              color:#6b7280; margin-bottom:2px; }
  .msg .body { white-space:pre-wrap; word-break:break-word; }
  .msg.user .body { color:#9db4ff; }
  .msg .think { white-space:pre-wrap; word-break:break-word; color:#6b7280; font-style:italic;
                border-left:2px solid #2a313b; padding-left:8px; margin-bottom:6px; display:none; }
  .msg .think.show { display:block; }
  .msg .err { color:#ff6b6b; }
  .chatstats { font-size:11px; color:#8b94a3; margin-top:6px; min-height:16px; }
  .chatstats b { color:#e8edf3; font-weight:600; }
  .chatrow { display:flex; gap:8px; margin-top:8px; align-items:stretch; }
  .chatrow textarea { flex:1; min-height:44px; }
  .chatbtns { display:flex; flex-direction:column; gap:6px; }
  .chatbtns button { margin-top:0; width:auto; padding:8px 14px; }
  .top { display:flex; align-items:flex-start; justify-content:space-between; gap:10px; }
  .lang-switch { background:none; border:1px solid #2a313b; color:#8b94a3; border-radius:6px; padding:3px 8px;
                 font-size:11px; font-weight:400; }
</style>
</head>
<body>
<div class="wrap">
  <div class="top"><div><h1>OLLAMAPROXY</h1>
  <div class="sub" id="upstream">verbinde…</div></div><span id="lang"></span></div>

  <div class="card">
    <label>Arbeitsspeicher</label>
    <div class="membar" id="membar"></div>
    <div class="legend">
      <span><i class="seg-models"></i>Modelle</span>
      <span><i class="seg-other"></i>System + andere Apps</span>
      <span><i style="background:#1b2027;border:1px solid #2a313b"></i>frei</span>
    </div>
    <div class="memstats" id="memstats"></div>
    <div class="note" id="swapnote"></div>

    <label style="margin-top:18px">Geladene Modelle</label>
    <div class="modellist" id="loadedlist"></div>
    <div class="note">„belegt“ = Prozess-RAM + GPU-Anteil (GTT/VRAM), mindestens aber die
      Prognose: Seit Ollama 0.40 liegen die GPU-Gewichte im Grafiktreiber (GTT, geteilt mit
      System-RAM) und tauchen nicht im Prozess-RSS auf. Ollama selbst meldet oft zu wenig.</div>
  </div>

  <div class="card">
    <label>Freigegebene Modelle</label>
    <div class="note" style="margin-top:0">Clients sehen nur diese Modelle. Jedes läuft
      immer mit seinen Settings – egal, was der Client mitschickt –, damit Ollama es nie
      neu laden muss. Anfragen mit unbekanntem Modellnamen landen beim Standardmodell (★).</div>
    <div id="entries"></div>
    <button class="ghost" style="margin-top:10px" onclick="addEntry()">+ Modell freigeben</button>

    <label style="margin-top:14px">Bild-/Audio-Helfer (für Modelle ohne Vision/Audio)</label>
    <select id="visionHelper"></select>
    <div class="note" style="margin-top:4px">Wird NUR genutzt, wenn das angefragte Modell das
      Medium (Bild bzw. Audio) nicht selbst verarbeiten kann. Beispiel: qwen3-coder-next bekommt
      Bilder + Audio von gemma4:e4b beschrieben/transkribiert; Agen/gemma-4-26B sieht Bilder selbst
      und nutzt den Helfer nur für Audio; gemma4:e4b verarbeitet beides direkt.</div>

    <div class="note" id="kvnote">KV-Cache-Typ: wird am Ollama-Server gesetzt
      (OLLAMA_KV_CACHE_TYPE) und gilt für alle Modelle.</div>
    <div class="note">Sampling (temperature, top_p, stop …) bestimmt weiterhin jeder
      Client selbst. think-Default greift nur, wenn der Client kein think mitschickt.
      Alias = Name, unter dem das Modell in Open WebUI / Home Assistant erscheint.</div>

    <button class="primary" onclick="apply()">Anwenden + Modelle vorladen</button>
    <div class="status" id="status"></div>
  </div>

  <div class="card">
    <label>Test-Chat</label>
    <div class="row" style="grid-template-columns:2fr 1fr">
      <select id="chatModel" title="Modell"></select>
      <select id="chatThink" title="think">
        <option value="">think: Default</option>
        <option value="true">think an</option>
        <option value="false">think aus</option>
      </select>
    </div>
    <div class="chatlog" id="chatlog"><span class="empty">Noch keine Nachrichten.</span></div>
    <div class="chatstats" id="chatstats"></div>
    <div class="chatrow">
      <textarea id="chatInput" rows="2"
        placeholder="Nachricht … (Enter = senden, Shift+Enter = neue Zeile)"></textarea>
      <div class="chatbtns">
        <button class="primary" id="chatSend" onclick="chatSend()">Senden</button>
        <button class="ghost" onclick="chatReset()">Neu</button>
      </div>
    </div>
    <div class="note">Läuft über den Proxy (/api/chat) – mit genau den erzwungenen
      Settings wie bei echten Clients. Während der Ausgabe: grobe Live-Rate; am Ende
      die exakten Werte von Ollama.</div>
  </div>

  <div class="card">
    <label>Modell installieren</label>
    <div class="saverow">
      <input id="pullName" placeholder="z. B. llama3.1:8b oder hf.co/…">
      <button class="ghost" onclick="pullModel()">Pull</button>
    </div>
    <div class="pullbar" id="pullbar"><div class="pullfill" id="pullfill"></div></div>
    <div class="status" id="pullstatus"></div>

    <label style="margin-top:18px">Installierte Modelle</label>
    <div class="modellist" id="modellist"></div>
  </div>

  <div class="card">
    <label>Presets</label>
    <div class="presets" id="presets"></div>
    <div class="saverow">
      <input id="presetName" placeholder="Preset-Name…">
      <button class="ghost" onclick="savePreset()">Speichern</button>
    </div>
  </div>
</div>

<script>
let AVAILABLE = [];
let ENTRIES = [];      // freigegebene Modelle (Bearbeitungsstand der UI)
let DEFAULT = "";      // Standardmodell
let VISION_HELPER = "";
let PROMOTED = {};     // Modellname -> Eintrag, wie er am Server gilt
let SIZES = {};
let FORCED_CTX = 8192;
let MEM = null;        // letzte /admin/memory-Antwort
i18nApply();
document.getElementById("lang").replaceWith(langSwitch());

async function load() {
  const s = await (await fetch("/admin/state")).json();
  AVAILABLE = s.available_models;
  ENTRIES = JSON.parse(JSON.stringify(s.models));
  DEFAULT = s.default_model;
  VISION_HELPER = s.vision_helper || "";
  PROMOTED = Object.fromEntries(s.models.map(e => [e.model, e]));
  document.getElementById("upstream").textContent =
    tr("{n} Modelle am Upstream verfügbar · {m} freigegeben", {n: s.available_models.length, m: s.models.length}) +
    (s.version ? " · v" + s.version : "");
  SIZES = s.model_sizes || {};
  if (s.kv_cache_type) document.getElementById("kvnote").textContent =
    tr("KV-Cache-Typ: {type} – wird am Ollama-Server gesetzt (OLLAMA_KV_CACHE_TYPE) und gilt für alle Modelle.", {type: s.kv_cache_type});
  const d = PROMOTED[DEFAULT];
  FORCED_CTX = (d && d.forced_options.num_ctx) || 8192;
  renderEntries();
  renderVisionHelper();
  renderPresets(s.presets);
  renderModels();
  const cname = e => e.alias || e.model;
  fillChatModels([PROMOTED[DEFAULT]].concat(s.models.filter(e => e.model !== DEFAULT))
                 .filter(Boolean).map(cname));
}

const ctxOf = e => (e.forced_options && e.forced_options.num_ctx) || "";
const thinkVal = t => t === true ? "true" : t === false ? "false" : "null";

function parseKeepAlive(v) {
  v = String(v).trim();
  if (v === "" || v === "-1") return -1;
  return /^-?\\d+$/.test(v) ? parseInt(v) : v;     // Zahl (Sekunden) oder z.B. "30m"
}

function renderEntries() {
  const box = document.getElementById("entries");
  box.innerHTML = "";
  ENTRIES.forEach((e, i) => {
    const div = document.createElement("div");
    div.className = "entry" + (e.model === DEFAULT ? " is-default" : "");
    const opts = AVAILABLE.includes(e.model) ? AVAILABLE : [e.model].concat(AVAILABLE);
    div.innerHTML =
      `<div class="entry-head">
         <button class="star" title="${tr('Standardmodell')}">★</button>
         <select class="e-model">${opts.map(m => `<option value="${esc(m)}"${m === e.model ? " selected" : ""}>${esc(m)}${AVAILABLE.includes(m) ? "" : tr(" (nicht installiert)")}</option>`).join("")}</select>
         <button class="del" title="${tr('Freigabe entfernen')}">✕</button>
       </div>
       <div class="row3">
         <div><label>${tr('Alias (optional)')}</label><input class="e-alias" placeholder="${tr('echter Name')}"></div>
         <div><label>num_ctx</label><input class="e-ctx" type="number" step="512"></div>
         <div><label>keep_alive</label><input class="e-ka" placeholder="${tr('-1 = dauerhaft')}"></div>
       </div>
       <div class="row">
         <div><label>${tr('think (Standard)')}</label>
           <select class="e-think">
             <option value="null">${tr('Client entscheidet')}</option>
             <option value="true">${tr('an')}</option><option value="false">${tr('aus')}</option>
           </select></div>
         <div><label class="check"><input type="checkbox" class="e-warm"> ${tr('beim Anwenden vorladen')}</label></div>
       </div>
       <div><label>${tr('Profile (optional, eins pro Zeile: weiterer Name mit eigenen Sampling-Werten, gleicher Runner)')}</label>
         <textarea class="e-prof" rows="2" placeholder="${tr('Qwen3.6-Code: temperature=0.6 top_p=0.95 top_k=20 presence_penalty=0 think=an')}"></textarea></div>`;
    const q = c => div.querySelector(c);
    q(".e-alias").value = e.alias || "";
    q(".e-ctx").value = ctxOf(e);
    q(".e-ka").value = e.keep_alive;
    q(".e-think").value = thinkVal(e.think_default);
    q(".e-warm").checked = e.warm !== false;
    q(".e-prof").value = formatProfiles(e.profiles);
    q(".e-prof").oninput = ev => { e.profiles = parseProfiles(ev.target.value); };
    q(".e-model").onchange = ev => {
      if (DEFAULT === e.model) DEFAULT = ev.target.value;
      e.model = ev.target.value; renderEntries();
    };
    q(".e-alias").oninput = ev => { e.alias = ev.target.value.trim(); };
    q(".e-ctx").oninput = ev => {
      const v = parseInt(ev.target.value);
      e.forced_options = isNaN(v) ? {} : {num_ctx: v};
    };
    q(".e-ka").oninput = ev => { e.keep_alive = parseKeepAlive(ev.target.value); };
    q(".e-think").onchange = ev => {
      const v = ev.target.value; e.think_default = v === "true" ? true : v === "false" ? false : null;
    };
    q(".e-warm").onchange = ev => { e.warm = ev.target.checked; };
    q(".star").onclick = () => { DEFAULT = e.model; renderEntries(); };
    q(".del").onclick = () => removeEntry(i);
    box.appendChild(div);
  });
}

// Profile <-> Text: "Name: key=wert key=wert think=an|aus"
function formatProfiles(list) {
  return (list || []).map(p => p.alias + ":" +
    Object.entries(p.options || {}).map(([k, v]) => " " + k + "=" + v).join("") +
    (p.think === true ? " think=an" : p.think === false ? " think=aus" : "")).join("\\n");
}
function parseProfiles(text) {
  const out = [];
  for (const line of text.split("\\n")) {
    const m = line.match(/^\\s*([^:]+?)\\s*:(.*)$/);
    if (!m) continue;
    const p = {alias: m[1], options: {}, think: null};
    for (const kv of m[2].trim().split(/\\s+/)) {
      const [k, v] = kv.split("=");
      if (!k || v === undefined) continue;
      if (k === "think") p.think = /^(an|on|true|ja)$/i.test(v) ? true : /^(aus|off|false|nein)$/i.test(v) ? false : null;
      else if (v !== "" && !isNaN(Number(v))) p.options[k] = Number(v);
    }
    out.push(p);
  }
  return out;
}

function addEntry() {
  const used = new Set(ENTRIES.map(e => e.model));
  const model = AVAILABLE.find(m => !used.has(m));
  if (!model) { alert(tr("Alle installierten Modelle sind schon freigegeben.")); return; }
  ENTRIES.push({model, alias: "", forced_options: {num_ctx: FORCED_CTX},
                keep_alive: -1, think_default: null, warm: true, profiles: []});
  if (!DEFAULT) DEFAULT = model;
  renderEntries();
}

function removeEntry(i) {
  if (ENTRIES.length === 1) { alert(tr("Mindestens ein Modell muss freigegeben bleiben.")); return; }
  const [gone] = ENTRIES.splice(i, 1);
  if (gone.model === DEFAULT) DEFAULT = ENTRIES[0].model;
  renderEntries();
}

const gather = () => ({models: ENTRIES, default_model: DEFAULT, vision_helper: VISION_HELPER});

function renderVisionHelper() {
  const sel = document.getElementById("visionHelper");
  const opts = [""].concat(AVAILABLE.includes(VISION_HELPER) || !VISION_HELPER ? AVAILABLE : [VISION_HELPER].concat(AVAILABLE));
  sel.innerHTML = opts.map(m => `<option value="${esc(m)}"${m === VISION_HELPER ? " selected" : ""}>${m ? esc(m) + (AVAILABLE.includes(m) ? "" : tr(" (nicht installiert)")) : tr("aus (Bilder unverändert durchreichen)")}</option>`).join("");
  sel.onchange = ev => { VISION_HELPER = ev.target.value; };
}

async function apply() {
  const st = document.getElementById("status");
  st.textContent = tr("wird angewendet…"); st.style.color = "#9db4ff";
  const r = await fetch("/admin/apply", {method:"POST",
    headers:{"Content-Type":"application/json"}, body:JSON.stringify(gather())});
  const d = await r.json();
  if (!r.ok) { st.textContent = tr("Fehler: {error}", {error: d.error || r.status}); st.style.color = "#ff6b6b"; return; }
  st.textContent = tr("✓ angewendet") + (d.warming.length
    ? tr(" – lade vor: {models}", {models: d.warming.join(", ")}) : tr(" – alles mit Häkchen ist schon richtig geladen")) +
    (d.not_loaded_cold && d.not_loaded_cold.length
      ? tr(" · nicht geladen (Vorladen aus): {models}", {models: d.not_loaded_cold.join(", ")}) : "");
  st.style.color = "#6dd58c";
  await load();
  setTimeout(loadMem, 3000);   // Warmup laeuft im Hintergrund
}

function renderPresets(names) {
  const box = document.getElementById("presets");
  box.innerHTML = "";
  if (!names.length) { box.innerHTML =
    '<span style="color:#6b7280;font-size:12px">' + tr('noch keine Presets') + '</span>'; return; }
  names.forEach(n => {
    const c = document.createElement("span"); c.className = "chip";
    c.innerHTML = `<span class="load">${n}</span><button>✕</button>`;
    c.querySelector(".load").onclick = () => loadPreset(n);
    c.querySelector("button").onclick = () => delPreset(n);
    box.appendChild(c);
  });
}

async function loadPreset(name) {
  const all = await (await fetch("/admin/presets")).json();
  const p = all[name]; if (!p) return;
  ENTRIES = JSON.parse(JSON.stringify(p.models));
  DEFAULT = p.default_model;
  renderEntries();
  apply();
}

async function savePreset() {
  const name = document.getElementById("presetName").value.trim();
  if (!name) return;
  const r = await fetch("/admin/presets", {method:"POST",
    headers:{"Content-Type":"application/json"}, body:JSON.stringify({name, ...gather()})});
  const d = await r.json();
  if (!r.ok) { alert(tr("Speichern fehlgeschlagen: {error}", {error: d.error || r.status})); return; }
  document.getElementById("presetName").value = "";
  renderPresets(d.presets);
}

async function delPreset(name) {
  const r = await (await fetch("/admin/presets/" + encodeURIComponent(name),
    {method:"DELETE"})).json();
  renderPresets(r.presets);
}

function renderModels() {
  const box = document.getElementById("modellist");
  box.innerHTML = "";
  if (!AVAILABLE.length) { box.innerHTML =
    '<span style="color:#6b7280;font-size:12px">' + tr('keine Modelle gefunden') + '</span>'; return; }
  AVAILABLE.forEach(m => {
    const row = document.createElement("div"); row.className = "modelrow";
    const isPromoted = m in PROMOTED;
    row.innerHTML = `<span class="mname" title="${esc(m)}">${esc(m)}</span>` +
      (isPromoted ? '<span class="active-tag">' + tr('freigegeben') + '</span>'
                  : '<button title="' + tr('löschen') + '">✕</button>');
    if (!isPromoted) row.querySelector("button").onclick = () => delModel(m);
    box.appendChild(row);
  });
}

async function pullModel() {
  const name = document.getElementById("pullName").value.trim();
  if (!name) return;
  const bar = document.getElementById("pullbar");
  const fill = document.getElementById("pullfill");
  const st = document.getElementById("pullstatus");
  bar.classList.add("active"); fill.style.width = "0%";
  st.style.color = "#9db4ff"; st.textContent = tr("starte Download…");

  try {
    const resp = await fetch("/admin/pull", {method:"POST",
      headers:{"Content-Type":"application/json"}, body:JSON.stringify({name})});
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      buf += dec.decode(value, {stream:true});
      const lines = buf.split("\\n"); buf = lines.pop();
      for (const line of lines) {
        if (!line.trim()) continue;
        let o; try { o = JSON.parse(line); } catch { continue; }
        if (o.error) { st.style.color="#ff6b6b"; st.textContent=tr("Fehler: {error}", {error: o.error}); return; }
        if (o.total && o.completed) {
          const pct = Math.round(o.completed / o.total * 100);
          fill.style.width = pct + "%";
          st.textContent = (o.status||"") + " " + pct + "%";
        } else if (o.status) {
          st.textContent = o.status;
        }
      }
    }
    fill.style.width = "100%";
    st.style.color = "#6dd58c"; st.textContent = tr("✓ fertig installiert");
    document.getElementById("pullName").value = "";
    await load();   // Dropdown + Liste aktualisieren
    setTimeout(() => bar.classList.remove("active"), 1500);
  } catch (e) {
    st.style.color = "#ff6b6b"; st.textContent = tr("Fehler: {error}", {error: e});
  }
}

async function delModel(name) {
  if (!confirm(tr("Modell „{name}“ wirklich vom Server löschen?", {name}))) return;
  const r = await fetch("/admin/delete", {method:"POST",
    headers:{"Content-Type":"application/json"}, body:JSON.stringify({name})});
  if (!r.ok) {
    const e = await r.json();
    alert(tr("Löschen fehlgeschlagen: {error}", {error: e.error || r.status}));
    return;
  }
  await load();
}

// ---- Speicher ----

const GB = 1024 * 1024 * 1024;
const gb = b => (b / GB).toFixed(1) + " GB";
const esc = s => String(s).replace(/[&<>"]/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

async function loadMem() {
  try { MEM = await (await fetch("/admin/memory")).json(); } catch { return; }
  renderMem();
}

function renderMem() {
  const bar = document.getElementById("membar");
  const stats = document.getElementById("memstats");
  const m = MEM && MEM.memory;
  if (!m) {
    bar.innerHTML = "";
    stats.innerHTML = '<span style="color:#6b7280;font-size:12px">' + tr('RAM-Werte nicht verfügbar') + '</span>';
  } else {
    const models = Math.min(MEM.models_bytes, m.used);
    const other = Math.max(m.used - models, 0);
    const pct = b => (b / m.total * 100).toFixed(2) + "%";
    bar.innerHTML = `<div class="seg-models" style="width:${pct(models)}" title="${tr('Modelle {size}', {size: gb(models)})}"></div>` +
      `<div class="seg-other" style="width:${pct(other)}" title="${tr('System + Apps {size}', {size: gb(other)})}"></div>`;
    stats.innerHTML =
      `<div><span>${tr('Gesamt')}</span><b>${gb(m.total)}</b></div>` +
      `<div><span>${tr('Belegt')}</span><b>${gb(m.used)}</b></div>` +
      `<div><span>${tr('davon Modelle')}</span><b>${gb(MEM.models_bytes)}</b></div>` +
      `<div><span>${tr('Frei')}</span><b class="${m.available < 2*GB ? "warn" : ""}">${gb(m.available)}</b></div>`;
    const sw = document.getElementById("swapnote");
    const swPct = m.swap_total ? m.swap_used / m.swap_total : 0;
    // Swap allein ist kein Alarm: Linux lagert auch bei viel freiem RAM selten genutzte Seiten
    // aus (die bleiben dort, bis sie gebraucht werden). Eng wird es erst, wenn zusaetzlich wenig
    // RAM frei ist.
    const lowRam = m.available < Math.max(4*GB, 0.08 * m.total);
    const swState = swPct > 0.75 && lowRam ? "bad" : (swPct > 0.4 && lowRam) || (m.available < 2*GB) ? "warn" : "";
    sw.className = "note " + swState;
    let gpuTxt = "";
    if (m.gpu) gpuTxt = " · " + tr("GPU-Speicher (geteilt mit RAM): {used} / {total}", {used: gb(m.gpu.gtt_used + m.gpu.vram_used), total: gb(m.gpu.gtt_total + m.gpu.vram_total)}) +
      ` (GTT ${gb(m.gpu.gtt_used)} / ${gb(m.gpu.gtt_total)}, UMA-VRAM ${gb(m.gpu.vram_used)} / ${gb(m.gpu.vram_total)})`;
    else if (m.gpu_in_use != null) gpuTxt = " · " + tr("GPU-Speicher in Benutzung: {size}", {size: gb(m.gpu_in_use)});
    sw.textContent = tr("Swap: {used} von {total} belegt", {used: gb(m.swap_used), total: gb(m.swap_total)}) +
      (m.compressed ? " · " + tr("komprimiert: {size}", {size: gb(m.compressed)}) : "") + gpuTxt +
      (swState === "bad" ? tr(" – der Speicher ist am Limit, das System lagert aus")
       : swPct > 0.4 ? tr(" – ausgelagert sind nur selten genutzte Seiten, unkritisch solange genug RAM frei ist") : "");
  }

  const box = document.getElementById("loadedlist");
  const loaded = (MEM && MEM.loaded) || [];
  box.innerHTML = "";
  const mods = (MEM && MEM.modules) || [];
  const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  mods.forEach(md => {
    const row = document.createElement("div"); row.className = "loadedrow";
    const host = location.hostname;
    const href = l => l.url ? l.url
      : l.port ? `http://${host}:${l.port}${l.path || "/"}`
      : md.public_url ? md.public_url.replace(/[/]$/, "") + (l.path || "/")
      : `http://${host}:${md.port}${l.path || "/"}`;
    const links = md.links.map(l => `<a href="${esc(href(l))}" target="_blank" rel="noopener" style="color:#9db4ff">${esc(l.label)}</a>`);
    if (md.idle_free_minutes > 0 && md.capabilities.includes("free")) links.push(tr("gibt nach {n} Min. Leerlauf frei", {n: md.idle_free_minutes}));
    let st = '<span class="warn">' + tr('nicht erreichbar') + '</span>';
    if (md.reachable) {
      if (!md.capabilities.includes("memory")) st = tr("erreichbar");
      else if (md.reclaimable) {
        const sub = [];
        if (md.detail) sub.push(esc(md.detail));
        if (md.rss != null) sub.push("RAM " + gb(md.rss) + ", GPU " + gb(md.gpu || 0));
        st = tr("hält {size}", {size: gb(md.held)}) + (sub.length ? ` (${sub.join(" · ")})` : "");
      } else st = md.held ? tr("belegt {size}", {size: gb(md.held)}) : (md.detail ? esc(md.detail) : tr("nichts geladen")) +
        (md.rss != null && !md.held ? " " + tr("(Prozess {size})", {size: gb(md.rss)}) : "");
    }
    const canFree = md.reachable && md.capabilities.includes("free") && md.reclaimable;
    row.innerHTML = `<div class="info"><div class="mname">${esc(md.name)}</div>` +
      `<div class="meta">${st}${links.length ? " · " + links.join(" · ") : ""}</div></div>` +
      (canFree ? '<button class="ghost" style="padding:5px 10px;font-size:12px">' + tr('Freigeben') + '</button>' : "");
    const b = row.querySelector("button");
    if (b) b.onclick = async () => {
      b.disabled = true; b.textContent = tr("Gebe frei…");
      await fetch("/admin/modules/free", {method: "POST", headers: {"Content-Type": "application/json"},
                                          body: JSON.stringify({name: md.name})});
      await loadMem();
    };
    box.appendChild(row);
  });
  if (!loaded.length) {
    if (!mods.length) box.innerHTML = '<span style="color:#6b7280;font-size:12px">' + tr('kein Modell geladen') + '</span>';
    return;
  }
  loaded.forEach(l => {
    const row = document.createElement("div"); row.className = "loadedrow";
    const parts = [];
    if (l.context_length) parts.push("ctx " + l.context_length);
    parts.push(tr("belegt ~{size}", {size: gb(l.used || 0)}));
    if (l.gpu_bytes != null && l.footprint != null)
      parts.push(tr("Prozess-RAM {ram} + GPU (GTT) {gpu}", {ram: gb(l.footprint), gpu: gb(l.gpu_bytes)}));
    else if (l.footprint && l.estimate && l.footprint < l.estimate * 0.8)
      parts.push(tr("davon im Prozess {size}, Rest im GPU-Speicher", {size: gb(l.footprint)}));
    else if (l.footprint) parts.push(tr("gemessen {size}", {size: gb(l.footprint)}));
    if (l.ollama_size) parts.push(tr("Ollama meldet {size}", {size: gb(l.ollama_size)}));
    row.innerHTML = `<div class="info"><div class="mname" title="${esc(l.name)}">${esc(l.name)}` +
      (l.is_default ? ' <span class="tag def">★ ' + tr('Standard') + '</span>'
        : l.promoted ? ' <span class="tag promoted">' + tr('freigegeben') + '</span>' : "") + `</div>` +
      `<div class="meta">${parts.join(" · ")}</div></div>` +
      `<button class="ghost" style="padding:5px 10px;font-size:12px">${tr('Entladen')}</button>`;
    row.querySelector("button").onclick = () => unloadModel(l.name, l.promoted);
    box.appendChild(row);
  });
}

async function unloadModel(name, promoted) {
  if (promoted && !confirm(tr("{name} ist freigegeben. Entladen? Die nächste Anfrage lädt es neu.", {name}))) return;
  await fetch("/admin/unload", {method:"POST",
    headers:{"Content-Type":"application/json"}, body:JSON.stringify({model:name})});
  await loadMem();
}

// ---- Test-Chat ----

let CHAT = [];          // Verlauf [{role, content}]
let chatAbort = null;   // AbortController der laufenden Anfrage

function fillChatModels(names) {
  const sel = document.getElementById("chatModel");
  const prev = sel.value;
  sel.innerHTML = names.map(n => `<option value="${esc(n)}">${esc(n)}</option>`).join("");
  if (names.includes(prev)) sel.value = prev;
}

function chatReset() {
  if (chatAbort) chatAbort.abort();
  CHAT = [];
  document.getElementById("chatlog").innerHTML =
    '<span class="empty">' + tr('Noch keine Nachrichten.') + '</span>';
  document.getElementById("chatstats").textContent = "";
}

function chatAddMsg(role, text) {
  const log = document.getElementById("chatlog");
  const empty = log.querySelector(".empty");
  if (empty) empty.remove();
  const div = document.createElement("div");
  div.className = "msg " + role;
  div.innerHTML = `<div class="who">${role === "user" ? tr("Du") : tr("Modell")}</div>` +
    '<div class="think"></div><div class="body"></div>';
  div.querySelector(".body").textContent = text || "";
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
  return div;
}

const sec = ns => (ns / 1e9).toFixed(1) + " s";
const rate = (n, ns) => ns ? (n / (ns / 1e9)).toFixed(1) : "–";

async function chatSend() {
  if (chatAbort) { chatAbort.abort(); return; }      // Knopf ist waehrend der Ausgabe "Stop"
  const input = document.getElementById("chatInput");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  CHAT.push({role: "user", content: text});
  chatAddMsg("user", text);
  const msg = chatAddMsg("assistant", "");
  const bodyEl = msg.querySelector(".body"), thinkEl = msg.querySelector(".think");
  const stats = document.getElementById("chatstats");
  const btn = document.getElementById("chatSend");
  const log = document.getElementById("chatlog");

  const req = {model: document.getElementById("chatModel").value, messages: CHAT, stream: true};
  const tv = document.getElementById("chatThink").value;
  if (tv) req.think = tv === "true";

  chatAbort = new AbortController();
  btn.textContent = tr("Stop");
  stats.textContent = tr("warte auf erstes Token … (lädt evtl. das Modell)");
  const t0 = performance.now();
  let tFirst = null, nTok = 0, content = "", thinking = "", final = null;
  try {
    const resp = await fetch("/api/chat", {method: "POST", signal: chatAbort.signal,
      headers: {"Content-Type": "application/json"}, body: JSON.stringify(req)});
    if (!resp.ok) throw new Error((await resp.text()) || resp.status);
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      buf += dec.decode(value, {stream: true});
      const lines = buf.split("\\n"); buf = lines.pop();
      for (const line of lines) {
        if (!line.trim()) continue;
        let o; try { o = JSON.parse(line); } catch { continue; }
        if (o.error) throw new Error(o.error);
        const m = o.message || {};
        if (m.thinking || m.content) {
          if (tFirst === null) tFirst = performance.now();
          nTok++;                                   // Ollama streamt ~1 Token pro Zeile
          if (m.thinking) { thinking += m.thinking; thinkEl.textContent = thinking;
                            thinkEl.classList.add("show"); }
          if (m.content) { content += m.content; bodyEl.textContent = content; }
          log.scrollTop = log.scrollHeight;
          const dt = (performance.now() - tFirst) / 1000;
          stats.innerHTML = tr("live ~{rate} tok/s · {n} Tokens · erstes Token nach {s} s",
            {rate: `<b>${dt > 0.2 ? (nTok / dt).toFixed(1) : "…"}</b>`, n: nTok, s: ((tFirst - t0) / 1000).toFixed(1)});
        }
        if (o.done) final = o;
      }
    }
    CHAT.push({role: "assistant", content});
    if (final) {
      stats.innerHTML = esc(final.model || req.model) + " · " +
        tr("Antwort {rate} tok/s ({n} Tokens) · Prompt {prate} tok/s ({pn} Tokens) · erstes Token nach {first} · Laden {load} · gesamt {total}", {
          rate: `<b>${rate(final.eval_count, final.eval_duration)}</b>`, n: final.eval_count || 0,
          prate: `<b>${rate(final.prompt_eval_count, final.prompt_eval_duration)}</b>`, pn: final.prompt_eval_count || 0,
          first: tFirst ? ((tFirst - t0) / 1000).toFixed(1) + " s" : "–",
          load: sec(final.load_duration || 0), total: sec(final.total_duration || 0)}) +
        (final.done_reason && final.done_reason !== "stop" ? " · " + tr("Ende: {reason}", {reason: esc(final.done_reason)}) : "");
    }
  } catch (e) {
    if (e.name === "AbortError") {
      if (content) CHAT.push({role: "assistant", content});
      stats.textContent = tr("abgebrochen nach {n} Tokens", {n: nTok});
    } else {
      bodyEl.innerHTML = '<span class="err">' + tr("Fehler: {error}", {error: esc(e.message || e)}) + "</span>";
      CHAT.pop();                                  // fehlgeschlagene Frage nicht im Verlauf lassen
      stats.textContent = "";
    }
  } finally {
    chatAbort = null;
    btn.textContent = tr("Senden");
  }
}

document.getElementById("chatInput").addEventListener("keydown", ev => {
  if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); chatSend(); }
});

load();
loadMem();
setInterval(() => { if (!document.hidden) loadMem(); }, 5000);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    import uvicorn
    print(f"Upstream Ollama : {UPSTREAM}")
    print(f"Proxy hoert auf : http://{LISTEN_HOST}:{LISTEN_PORT}")
    print(f"Web-UI          : http://{LISTEN_HOST}:{LISTEN_PORT}/")
    uvicorn.run(app, host=LISTEN_HOST, port=LISTEN_PORT)
