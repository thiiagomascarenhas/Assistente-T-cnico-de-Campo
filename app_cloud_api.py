"""
Ponte local entre WhatsApp Cloud API (Meta, oficial) e Gemini API.

Fluxo:
  WhatsApp -> Meta Cloud API -> POST /webhook (este servidor)
           -> Gemini (system_instruction = system_prompt.txt)
           -> Meta Graph API (send message) -> WhatsApp

Arquivo separado de app.py (que ainda usa a Evolution API em producao).
Sera promovido a app.py quando o numero real for migrado e os testes
com o numero de teste da Meta confirmarem que tudo funciona.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
from pathlib import Path
from typing import List, Optional, Set, Tuple

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from google import genai
from google.genai import types

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bridge-cloud-api")

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

WHATSAPP_ACCESS_TOKEN = os.environ["WHATSAPP_ACCESS_TOKEN"]
WHATSAPP_PHONE_NUMBER_ID = os.environ["WHATSAPP_PHONE_NUMBER_ID"]
WHATSAPP_VERIFY_TOKEN = os.environ["WHATSAPP_VERIFY_TOKEN"]
WHATSAPP_APP_SECRET = os.environ.get("WHATSAPP_APP_SECRET")
GRAPH_API_VERSION = os.environ.get("GRAPH_API_VERSION", "v21.0")

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-lite-latest")

BRIDGE_HOST = os.environ.get("BRIDGE_HOST", "0.0.0.0")
CLOUD_API_BRIDGE_PORT = int(os.environ.get("CLOUD_API_BRIDGE_PORT", "8001"))

APP_LOGIN_USER = os.environ.get("APP_LOGIN_USER", "assistentetecnicodecampo")
APP_LOGIN_PASSWORD = os.environ.get("APP_LOGIN_PASSWORD", "12345678")
SESSION_COOKIE_NAME = "ateg_session"

SYSTEM_PROMPT = (BASE_DIR / "system_prompt.txt").read_text(encoding="utf-8")
TEST_CHAT_PAGE = (BASE_DIR / "test_chat.html").read_text(encoding="utf-8")
LOGIN_PAGE = (BASE_DIR / "login.html").read_text(encoding="utf-8")

gemini_client = genai.Client(api_key=GEMINI_API_KEY)

app = FastAPI(title="WhatsApp Cloud API <-> Gemini Bridge")

HISTORY_TURNS_LIMIT = 20

# Historico de conversa persistido em disco (sqlite), por numero de telefone
# ou "test:<session_id>" para a pagina web. DATA_DIR aponta para o volume
# persistente em producao (Fly.io); em dev local, cai no diretorio do projeto.
DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR)))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "chat_histories.db"


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_histories (
            session_id TEXT PRIMARY KEY,
            history_json TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    return conn


def load_history(session_id: str) -> List[types.Content]:
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT history_json FROM chat_histories WHERE session_id = ?", (session_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return []
    return [types.Content.model_validate(item) for item in json.loads(row[0])]


def save_history(session_id: str, history: List[types.Content]) -> None:
    history_json = json.dumps([c.model_dump(mode="json") for c in history])
    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO chat_histories (session_id, history_json, updated_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(session_id) DO UPDATE SET
                history_json = excluded.history_json,
                updated_at = excluded.updated_at
            """,
            (session_id, history_json),
        )
        conn.commit()
    finally:
        conn.close()

# Sessoes de login validas, em memoria (perdidas ao reiniciar o processo).
active_sessions: Set[str] = set()


def is_authenticated(request: Request) -> bool:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    return bool(token) and token in active_sessions


def verify_signature(raw_body: bytes, signature_header: Optional[str]) -> bool:
    """Confere a assinatura X-Hub-Signature-256 que a Meta envia em cada POST do webhook."""
    if not WHATSAPP_APP_SECRET:
        return True
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(WHATSAPP_APP_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header.removeprefix("sha256="))


def extract_message_content(message: dict) -> Optional[dict]:
    """Retorna {'text': str|None, 'media_id': str|None, 'mime_type': str|None},
    ou None se o tipo de mensagem nao for suportado."""
    msg_type = message.get("type")
    if msg_type == "text":
        return {"text": message.get("text", {}).get("body"), "media_id": None, "mime_type": None}
    if msg_type == "interactive":
        interactive = message.get("interactive", {})
        if "button_reply" in interactive:
            return {"text": interactive["button_reply"].get("title"), "media_id": None, "mime_type": None}
        if "list_reply" in interactive:
            return {"text": interactive["list_reply"].get("title"), "media_id": None, "mime_type": None}
        return None
    if msg_type == "audio":
        audio = message.get("audio", {})
        return {"text": None, "media_id": audio.get("id"), "mime_type": audio.get("mime_type")}
    if msg_type == "image":
        image = message.get("image", {})
        return {"text": image.get("caption"), "media_id": image.get("id"), "mime_type": image.get("mime_type")}
    return None


async def download_whatsapp_media(media_id: str) -> Tuple[bytes, str]:
    """Busca a URL temporaria da midia pelo media id e baixa o conteudo."""
    headers = {"Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}"}
    async with httpx.AsyncClient(timeout=30) as client:
        meta_resp = await client.get(f"https://graph.facebook.com/{GRAPH_API_VERSION}/{media_id}", headers=headers)
        meta_resp.raise_for_status()
        media_info = meta_resp.json()

        file_resp = await client.get(media_info["url"], headers=headers)
        file_resp.raise_for_status()
        return file_resp.content, media_info.get("mime_type", "application/octet-stream")


def brazil_number_variants(number: str) -> List[str]:
    """Numeros brasileiros podem chegar do webhook com ou sem o 9o digito do
    celular, dependendo de como o WhatsApp normaliza internamente, mas isso
    nem sempre bate com o formato esperado para enviar de volta. Gera as
    variantes possiveis para tentar, na ordem: original, depois a alternativa."""
    match = re.fullmatch(r"55(\d{2})(\d{8,9})", number)
    if not match:
        return [number]

    ddd, subscriber = match.groups()
    variants = [number]
    if len(subscriber) == 8:
        variants.append(f"55{ddd}9{subscriber}")
    elif len(subscriber) == 9 and subscriber[0] == "9":
        variants.append(f"55{ddd}{subscriber[1:]}")
    return variants


async def send_whatsapp_text(to_number: str, text: str) -> None:
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }

    last_error: Optional[Exception] = None
    async with httpx.AsyncClient(timeout=30) as client:
        for candidate in brazil_number_variants(to_number):
            payload = {
                "messaging_product": "whatsapp",
                "to": candidate,
                "type": "text",
                "text": {"body": text},
            }
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code < 400:
                return
            logger.warning("Envio para %s falhou (%s): %s", candidate, resp.status_code, resp.text)
            last_error = httpx.HTTPStatusError(
                f"{resp.status_code} enviando para {candidate}: {resp.text}",
                request=resp.request,
                response=resp,
            )

    if last_error:
        raise last_error


def ask_gemini(session_id: str, user_text: Optional[str], media: Optional[Tuple[bytes, str]] = None) -> str:
    history = load_history(session_id)

    parts = []
    if media:
        media_bytes, mime_type = media
        parts.append(types.Part.from_bytes(data=media_bytes, mime_type=mime_type))
    if user_text:
        parts.append(types.Part(text=user_text))
    history.append(types.Content(role="user", parts=parts))

    response = gemini_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=history,
        config=types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT),
    )

    reply_text = response.text or ""
    history.append(types.Content(role="model", parts=[types.Part(text=reply_text)]))
    save_history(session_id, history[-HISTORY_TURNS_LIMIT:])
    return reply_text


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return LOGIN_PAGE


@app.post("/login")
async def login(request: Request):
    payload = await request.json()
    username = payload.get("username", "")
    password = payload.get("password", "")

    if username != APP_LOGIN_USER or password != APP_LOGIN_PASSWORD:
        return JSONResponse({"error": "Usuário ou senha incorretos."}, status_code=401)

    token = secrets.token_urlsafe(32)
    active_sessions.add(token)
    response = JSONResponse({"ok": True})
    response.set_cookie(
        SESSION_COOKIE_NAME, token, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30
    )
    return response


@app.post("/logout")
async def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE_NAME)
    active_sessions.discard(token)
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


@app.get("/test", response_class=HTMLResponse)
async def test_page(request: Request):
    if not is_authenticated(request):
        return RedirectResponse("/login")
    return TEST_CHAT_PAGE


@app.get("/test/history")
async def test_history(request: Request, session_id: str):
    if not is_authenticated(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    history = load_history(f"test:{session_id}")
    messages = []
    for content in history:
        text_parts = []
        has_image = False
        has_audio = False
        for part in content.parts or []:
            if part.text:
                text_parts.append(part.text)
            elif part.inline_data:
                mime = part.inline_data.mime_type or ""
                has_image = has_image or mime.startswith("image/")
                has_audio = has_audio or mime.startswith("audio/")
        messages.append(
            {
                "who": "user" if content.role == "user" else "bot",
                "text": " ".join(text_parts) if text_parts else None,
                "has_image": has_image,
                "has_audio": has_audio,
            }
        )
    return JSONResponse({"messages": messages})


@app.post("/test/chat")
async def test_chat(request: Request):
    if not is_authenticated(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    payload = await request.json()
    session_id = payload.get("session_id")
    text = payload.get("text")
    media_base64 = payload.get("media_base64")
    media_mime_type = payload.get("media_mime_type")

    if not session_id or (not text and not media_base64):
        return JSONResponse({"error": "session_id and text or media are required"}, status_code=400)

    media = (base64.b64decode(media_base64), media_mime_type) if media_base64 and media_mime_type else None

    try:
        reply_text = ask_gemini(f"test:{session_id}", text, media)
    except Exception:
        logger.exception("Falha ao processar mensagem de teste (session=%s)", session_id)
        return JSONResponse({"error": "Não foi possível processar essa mensagem/arquivo."}, status_code=502)
    return JSONResponse({"reply": reply_text})


@app.get("/webhook")
async def webhook_verify(request: Request):
    """Handshake de verificacao exigido pela Meta ao registrar a URL do webhook."""
    params = request.query_params
    if params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == WHATSAPP_VERIFY_TOKEN:
        return PlainTextResponse(params.get("hub.challenge", ""))
    return JSONResponse({"error": "verification failed"}, status_code=403)


@app.post("/webhook")
async def webhook(request: Request):
    raw_body = await request.body()
    if not verify_signature(raw_body, request.headers.get("x-hub-signature-256")):
        return JSONResponse({"error": "invalid signature"}, status_code=401)

    payload = await request.json()

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for message in value.get("messages", []):
                from_number = message.get("from")
                content = extract_message_content(message)

                if not from_number or not content:
                    continue

                user_text = content["text"]
                media: Optional[Tuple[bytes, str]] = None

                if content["media_id"]:
                    try:
                        media = await download_whatsapp_media(content["media_id"])
                    except Exception:
                        logger.exception("Falha ao baixar midia de %s", from_number)
                        await send_whatsapp_text(
                            from_number,
                            "Não consegui baixar o arquivo enviado. Pode tentar reenviar ou descrever em texto?",
                        )
                        continue

                if not user_text and not media:
                    continue

                try:
                    reply_text = ask_gemini(from_number, user_text, media)
                    if reply_text:
                        await send_whatsapp_text(from_number, reply_text)
                except Exception:
                    logger.exception("Falha ao processar mensagem de %s", from_number)

            for status in value.get("statuses", []):
                logger.info(
                    "Status de mensagem: id=%s status=%s destinatario=%s erro=%s",
                    status.get("id"),
                    status.get("status"),
                    status.get("recipient_id"),
                    status.get("errors"),
                )

    return JSONResponse({"ok": True})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app_cloud_api:app", host=BRIDGE_HOST, port=CLOUD_API_BRIDGE_PORT, reload=True)
