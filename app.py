"""
Ponte local entre WhatsApp (Evolution API v2) e Gemini API.

Fluxo:
  WhatsApp -> Evolution API -> POST /webhook (este servidor)
           -> Gemini (system_instruction = system_prompt.txt)
           -> Evolution API sendText -> WhatsApp
"""

import asyncio
import logging
import os
import random
from pathlib import Path
from typing import Dict, List, Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from google import genai
from google.genai import types

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bridge")

BASE_DIR = Path(__file__).resolve().parent


def load_dotenv_if_present() -> None:
    """Carrega .env manualmente (sem dependencia extra), sem sobrescrever env vars ja definidas."""
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


load_dotenv_if_present()

EVOLUTION_API_URL = os.environ["EVOLUTION_API_URL"].rstrip("/")
EVOLUTION_API_KEY = os.environ["EVOLUTION_API_KEY"]
EVOLUTION_INSTANCE = os.environ["EVOLUTION_INSTANCE"]

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")

BRIDGE_HOST = os.environ.get("BRIDGE_HOST", "0.0.0.0")
BRIDGE_PORT = int(os.environ.get("BRIDGE_PORT", "8000"))

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")

SYSTEM_PROMPT = (BASE_DIR / "system_prompt.txt").read_text(encoding="utf-8")
TEST_CHAT_PAGE = (BASE_DIR / "test_chat.html").read_text(encoding="utf-8")

gemini_client = genai.Client(api_key=GEMINI_API_KEY)

app = FastAPI(title="WhatsApp <-> Gemini Bridge")

# Historico de conversa em memoria, por remoteJid (perdido ao reiniciar o processo).
chat_histories: Dict[str, List[types.Content]] = {}
HISTORY_TURNS_LIMIT = 20


def extract_text(message: Optional[dict]) -> Optional[str]:
    if not message:
        return None
    if "conversation" in message:
        return message["conversation"]
    if "extendedTextMessage" in message:
        return message["extendedTextMessage"].get("text")
    if "imageMessage" in message and message["imageMessage"].get("caption"):
        return message["imageMessage"]["caption"]
    return None


async def send_whatsapp_text(number: str, text: str) -> None:
    url = f"{EVOLUTION_API_URL}/message/sendText/{EVOLUTION_INSTANCE}"
    headers = {"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"}
    payload = {"number": number, "text": text}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()


async def mark_as_read(key: dict) -> None:
    """Marca a mensagem recebida como lida, como um humano abrindo a conversa."""
    if not key.get("id") or not key.get("remoteJid"):
        return
    url = f"{EVOLUTION_API_URL}/chat/markMessageAsRead/{EVOLUTION_INSTANCE}"
    headers = {"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"}
    payload = {
        "readMessages": [
            {"id": key["id"], "fromMe": False, "remoteJid": key["remoteJid"]}
        ]
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
    except Exception:
        logger.warning("Falha ao marcar mensagem como lida", exc_info=True)


async def send_presence(number: str, presence: str, delay_ms: int) -> None:
    """Mostra o indicador 'digitando...'/'gravando...' antes de responder."""
    url = f"{EVOLUTION_API_URL}/chat/sendPresence/{EVOLUTION_INSTANCE}"
    headers = {"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"}
    payload = {"number": number, "presence": presence, "delay": delay_ms}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
    except Exception:
        logger.warning("Falha ao enviar presence '%s'", presence, exc_info=True)


def typing_delay_ms(text: str) -> int:
    """Estima um tempo de digitacao humano (com variacao) para o tamanho da resposta."""
    ms_per_char = random.uniform(35, 55)
    base = len(text) * ms_per_char
    jitter = base * random.uniform(-0.15, 0.15)
    return int(min(max(base + jitter, 1200), 6000))


def ask_gemini(remote_jid: str, user_text: str) -> str:
    history = chat_histories.setdefault(remote_jid, [])
    history.append(types.Content(role="user", parts=[types.Part(text=user_text)]))

    response = gemini_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=history,
        config=types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT),
    )

    reply_text = response.text or ""
    history.append(types.Content(role="model", parts=[types.Part(text=reply_text)]))
    chat_histories[remote_jid] = history[-HISTORY_TURNS_LIMIT:]
    return reply_text


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/test", response_class=HTMLResponse)
async def test_page():
    return TEST_CHAT_PAGE


@app.post("/test/chat")
async def test_chat(request: Request):
    payload = await request.json()
    session_id = payload.get("session_id")
    text = payload.get("text")

    if not session_id or not text:
        return JSONResponse({"error": "session_id and text are required"}, status_code=400)

    reply_text = ask_gemini(f"test:{session_id}", text)
    return JSONResponse({"reply": reply_text})


@app.post("/webhook")
async def webhook(request: Request):
    if WEBHOOK_SECRET and request.query_params.get("secret") != WEBHOOK_SECRET:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    payload = await request.json()
    event = payload.get("event")

    if event != "messages.upsert":
        return JSONResponse({"ignored": event})

    data = payload.get("data", {})
    key = data.get("key", {})

    if key.get("fromMe"):
        return JSONResponse({"ignored": "own message"})

    remote_jid = key.get("remoteJid")
    user_text = extract_text(data.get("message"))

    if not remote_jid or not user_text:
        return JSONResponse({"ignored": "no text content"})

    number = remote_jid.split("@")[0]

    try:
        # Pequena pausa antes de "notar" a mensagem, como um humano faria.
        await asyncio.sleep(random.uniform(0.4, 1.6))
        await mark_as_read(key)

        reply_text = ask_gemini(remote_jid, user_text)
        if reply_text:
            delay_ms = typing_delay_ms(reply_text)
            await send_presence(number, "composing", delay_ms)
            await asyncio.sleep(delay_ms / 1000)
            await send_whatsapp_text(number, reply_text)
    except Exception:
        logger.exception("Falha ao processar mensagem de %s", remote_jid)
        return JSONResponse({"error": "internal error"}, status_code=500)

    return JSONResponse({"ok": True})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host=BRIDGE_HOST, port=BRIDGE_PORT, reload=True)
