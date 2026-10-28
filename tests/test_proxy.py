"""Tests fuer ollama_proxy.py gegen ein nachgebautes Ollama (tests/fake_ollama.py).

    pip install -r requirements.txt pytest
    python -m pytest tests
"""
import json
import os
import socket
import sys
import tempfile
import threading
import time

import pytest
import uvicorn


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


PORT = _free_port()
TMP = tempfile.mkdtemp(prefix="proxytest-")
STATE = {
    "models": [
        {"model": "gemma4:test", "alias": "Gemma", "forced_options": {"num_ctx": 64000}, "keep_alive": -1,
         "think_default": False, "warm": False},
        {"model": "qwen:test", "alias": "", "forced_options": {"num_ctx": 32000}, "keep_alive": "15m",
         "think_default": None, "warm": False,
         "profiles": [{"alias": "Qwen-Code", "options": {"temperature": 0.6, "top_k": 20, "presence_penalty": 0}}]},
    ],
    "default_model": "gemma4:test",
    "vision_helper": "",
    "presets": {},
}
with open(os.path.join(TMP, "state.json"), "w") as f:
    json.dump(STATE, f)
with open(os.path.join(TMP, "modules.json"), "w") as f:
    json.dump({"modules": [{"url": f"http://127.0.0.1:{PORT}"}]}, f)
os.environ.update(OLLAMA_UPSTREAM=f"http://127.0.0.1:{PORT}", PROXY_STATE=os.path.join(TMP, "state.json"),
                  PROXY_MODULES_FILE=os.path.join(TMP, "modules.json"), PROXY_KEEP_LOADED_INTERVAL="0",
                  PROXY_MAX_PREDICT="0", PROXY_MAX_PREDICT_CLIENTS="testclient=100")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fake_ollama  # noqa: E402
import ollama_proxy as px  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def fake_server():
    srv = uvicorn.Server(uvicorn.Config(fake_ollama.app, host="127.0.0.1", port=PORT, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield
    srv.should_exit = True


@pytest.fixture(scope="session")
def client():
    # ein TestClient (= ein Event-Loop) fuer alle Tests: der Proxy haelt einen globalen httpx-Client
    with TestClient(px.app) as c:
        yield c


@pytest.fixture
def tc(client):
    fake_ollama.reset()
    px._module_info.clear()
    yield client


def sent(path="/api/chat") -> dict:
    return fake_ollama.S["last"][path]


# ---------------------------------------------------------------- Modellwahl und Erzwingen

def test_alias_forces_settings(tc):
    r = tc.post("/api/chat", json={"model": "Gemma", "stream": False, "keep_alive": "5m",
                                   "options": {"num_ctx": 2048, "temperature": 0.3},
                                   "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200 and r.json()["message"]["content"] == "Hallo"
    b = sent()
    assert b["model"] == "gemma4:test" and b["keep_alive"] == -1
    assert b["options"]["num_ctx"] == 64000 and b["options"]["temperature"] == 0.3   # Sampling bleibt
    assert b["think"] is False                                                         # think-Vorgabe


def test_unknown_model_goes_to_default_and_latest_tolerant(tc):
    tc.post("/api/chat", json={"model": "gibt-es-nicht", "stream": False, "messages": []})
    assert sent()["model"] == "gemma4:test"
    tc.post("/api/chat", json={"model": "qwen:test", "stream": False, "messages": []})
    assert sent()["model"] == "qwen:test" and sent()["keep_alive"] == "15m"
    assert px.find_entry("Gemma:latest")["model"] == "gemma4:test"


def test_client_think_wins(tc):
    tc.post("/api/chat", json={"model": "Gemma", "stream": False, "think": True, "messages": []})
    assert sent()["think"] is True


def test_neutral_penalties_dropped_real_values_kept(tc):
    tc.post("/api/chat", json={"model": "qwen:test", "stream": False, "messages": [],
                               "options": {"presence_penalty": 0, "frequency_penalty": 0.0,
                                           "repeat_penalty": 1, "top_p": 0.9}})
    o = sent()["options"]
    assert "presence_penalty" not in o and "frequency_penalty" not in o and "repeat_penalty" not in o
    assert o["top_p"] == 0.9
    tc.post("/api/chat", json={"model": "qwen:test", "stream": False, "messages": [],
                               "options": {"presence_penalty": 1.2}})
    assert sent()["options"]["presence_penalty"] == 1.2


def test_profile_fills_only_missing_values(tc):
    tc.post("/api/chat", json={"model": "Qwen-Code", "stream": False, "messages": []})
    o = sent()["options"]
    assert sent()["model"] == "qwen:test" and o["temperature"] == 0.6 and o["top_k"] == 20
    assert o["num_ctx"] == 32000 and o["presence_penalty"] == 0
    tc.post("/api/chat", json={"model": "Qwen-Code", "stream": False, "messages": [],
                               "options": {"temperature": 0.2}})
    assert sent()["options"]["temperature"] == 0.2


def test_max_predict_per_client(tc):
    # TestClient meldet sich als Host "testclient" -> Grenze 100 (PROXY_MAX_PREDICT_CLIENTS)
    tc.post("/api/generate", json={"model": "Gemma", "stream": False, "prompt": "x"})
    assert sent("/api/generate")["options"]["num_predict"] == 100
    tc.post("/api/generate", json={"model": "Gemma", "stream": False, "prompt": "x", "options": {"num_predict": 10}})
    assert sent("/api/generate")["options"]["num_predict"] == 10


# ---------------------------------------------------------------- Discovery

def test_tags_only_released_default_first(tc):
    names = [m["name"] for m in tc.get("/api/tags").json()["models"]]
    assert names[0] == "Gemma" and "qwen:test" in names and "Qwen-Code" in names
    assert "nicht-freigegeben:latest" not in names
    ids = [m["id"] for m in tc.get("/v1/models").json()["data"]]
    assert ids[0] == "Gemma" and "nicht-freigegeben:latest" not in ids


# ---------------------------------------------------------------- OpenAI -> nativ

def test_openai_chat_is_translated_to_native(tc):
    r = tc.post("/v1/chat/completions", json={
        "model": "Qwen-Code", "max_tokens": 50, "reasoning_effort": "high", "presence_penalty": 0,
        "messages": [{"role": "system", "content": "kurz"}, {"role": "user", "content": "hi"}]})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["model"] == "Qwen-Code" and j["choices"][0]["message"]["content"] == "Hallo"
    assert j["usage"]["completion_tokens"] == 3
    b = sent()
    assert b["model"] == "qwen:test" and b["options"]["num_ctx"] == 32000 and b["options"]["num_predict"] == 50
    assert b["think"] in (True, "high") and b["options"]["presence_penalty"] == 0   # aus dem Profil
    assert b["messages"][0] == {"role": "system", "content": "kurz"}


def test_openai_tool_calls_keep_ollama_ids(tc):
    fake_ollama.S["tool_calls"] = [{"id": "call_abc", "function": {"index": 0, "name": "wetter",
                                                                   "arguments": {"ort": "Berlin"}}}]
    j = tc.post("/v1/chat/completions", json={"model": "Gemma", "messages": [{"role": "user", "content": "?"}],
                                              "tools": [{"type": "function", "function": {"name": "wetter"}}]}).json()
    call = j["choices"][0]["message"]["tool_calls"][0]
    assert call["id"] == "call_abc" and call["function"]["name"] == "wetter"
    assert json.loads(call["function"]["arguments"]) == {"ort": "Berlin"}
    assert j["choices"][0]["finish_reason"] == "tool_calls"
    # Ergebnis im Verlauf: tool_call_id geht an Ollama zurueck
    tc.post("/v1/chat/completions", json={"model": "Gemma", "messages": [
        {"role": "user", "content": "?"},
        {"role": "assistant", "content": "", "tool_calls": [call]},
        {"role": "tool", "tool_call_id": "call_abc", "content": "sonnig"}]})
    msgs = sent()["messages"]
    assert msgs[1]["tool_calls"][0]["function"]["arguments"] == {"ort": "Berlin"}
    assert msgs[2]["role"] == "tool" and msgs[2].get("tool_call_id") == "call_abc"


def test_openai_stream_with_reasoning(tc):
    fake_ollama.S["thinking"] = "ueberlege"
    with tc.stream("POST", "/v1/chat/completions", json={"model": "Gemma", "stream": True,
                                                          "stream_options": {"include_usage": True},
                                                          "messages": [{"role": "user", "content": "hi"}]}) as r:
        lines = [ln for ln in r.iter_lines() if ln.startswith("data: ")]
    assert lines[-1] == "data: [DONE]"
    chunks = [json.loads(ln[6:]) for ln in lines[:-1]]
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks if c.get("choices"))
    reasoning = "".join(c["choices"][0]["delta"].get("reasoning_content", "") for c in chunks if c.get("choices"))
    assert text == "Hallo" and reasoning == "ueberlege"
    assert any(c.get("usage") for c in chunks)
    assert sent()["stream"] is True and sent()["options"]["num_ctx"] == 64000


def test_openai_completions(tc):
    j = tc.post("/v1/completions", json={"model": "qwen:test", "prompt": "a", "max_tokens": 5}).json()
    assert j["choices"][0]["text"] == "Text"
    assert sent("/api/generate")["options"]["num_predict"] == 5


# ---------------------------------------------------------------- Module

def test_modules_in_memory_view_and_free(tc):
    mods = tc.get("/admin/modules").json()
    assert len(mods) == 1
    m = mods[0]
    assert m["name"] == "Testmodul" and m["reachable"] and m["held"] == 5 * 1024 ** 3
    assert m["links"][0]["label"] == "Oeffnen" and m["port"] == PORT
    mem = tc.get("/admin/memory").json()
    assert mem["modules"][0]["name"] == "Testmodul"
    assert mem["models_bytes"] >= 5 * 1024 ** 3
    r = tc.post("/admin/modules/free", json={"name": "Testmodul"})
    assert r.status_code == 200 and fake_ollama.S["module_freed"] == 1
    assert tc.post("/admin/modules/free", json={"name": "gibtsnicht"}).status_code == 404


def test_legacy_env_becomes_modules(monkeypatch):
    monkeypatch.setenv("COMFYUI_URL", "http://host.docker.internal:8188")
    monkeypatch.setenv("WEBAPP_PUBLIC_PORT", "8190")
    monkeypatch.setenv("FACEREC_PUBLIC_PORT", "0")
    monkeypatch.setenv("COMFYUI_IDLE_FREE_MINUTES", "5")
    mods = px._legacy_modules()
    assert mods == [{"name": "ImageGen", "url": "http://host.docker.internal:8190", "idle_free_minutes": 5.0}]


def test_unreachable_module_is_reported(tc, monkeypatch):
    monkeypatch.setattr(px, "MODULES", [{"name": "weg", "url": "http://127.0.0.1:9"}])
    m = tc.get("/admin/modules").json()[0]
    assert m["name"] == "weg" and m["reachable"] is False and m["held"] == 0


# ---------------------------------------------------------------- Oberflaeche

def test_ui_served(tc):
    r = tc.get("/")
    assert r.status_code == 200 and "OllamaProxy" in r.text
