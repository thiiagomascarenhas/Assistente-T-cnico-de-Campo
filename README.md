# Ponte WhatsApp (Evolution API v2) <-> Gemini

Servidor local que recebe mensagens do WhatsApp via webhook da Evolution API,
responde usando o Gemini (`gemini-2.5-flash`) com as System Instructions de
`system_prompt.txt`, e devolve a resposta pelo WhatsApp.

Arquitetura: `Evolution API` (docker) -> `app.py` (host, fora do docker) -> `Gemini API`.

## Arquivos

- `app.py` — servidor FastAPI (webhook + ponte com Gemini)
- `docker-compose.yml` — Evolution API v2 + Postgres + Redis
- `requirements.txt` — dependencias Python
- `system_prompt.txt` — cole aqui as System Instructions do seu Gem
- `.env.example` — modelo de variaveis de ambiente

Comandos abaixo em **PowerShell**, a partir da pasta do projeto:

```powershell
cd C:\Users\Thiago\whatsapp-gemini-bridge
```

## 1. Configurar variaveis de ambiente

```powershell
Copy-Item .env.example .env
notepad .env
```

Preencha pelo menos:
- `EVOLUTION_GLOBAL_API_KEY` — chave que voce escolher para proteger a Evolution API
- `GEMINI_API_KEY` — sua chave da API do Gemini (https://aistudio.google.com/apikey)

## 2. Colar o system prompt do seu Gem

Abra `system_prompt.txt` e substitua o conteudo pelas System Instructions
exatas do seu Gem.

```powershell
notepad system_prompt.txt
```

## 3. Subir Evolution API + Postgres + Redis

```powershell
docker compose up -d
docker compose ps
```

A Evolution API fica disponivel em `http://localhost:8080` e o Manager (UI) em
`http://localhost:8080/manager`.

## 4. Criar a instancia do WhatsApp

```powershell
$env:GLOBAL_KEY = "changeme-global-api-key"   # mesmo valor de EVOLUTION_GLOBAL_API_KEY no .env

Invoke-RestMethod -Method Post `
  -Uri "http://localhost:8080/instance/create" `
  -Headers @{ apikey = $env:GLOBAL_KEY } `
  -ContentType "application/json" `
  -Body (@{
      instanceName = "my-instance"
      qrcode       = $true
      integration  = "WHATSAPP-BAILEYS"
  } | ConvertTo-Json)
```

Guarde o `hash`/`apikey` da instancia retornado na resposta — use-o em
`EVOLUTION_API_KEY` no `.env` (ou reutilize a global key, se sua instancia
estiver configurada para aceitar a global key).

Para pegar o QR Code e conectar o WhatsApp:

```powershell
Invoke-RestMethod -Method Get `
  -Uri "http://localhost:8080/instance/connect/my-instance" `
  -Headers @{ apikey = $env:GLOBAL_KEY }
```

Ou abra o Manager em `http://localhost:8080/manager` e escaneie o QR Code
pela interface.

## 5. Criar o ambiente Python e instalar dependencias

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 6. Rodar a ponte (app.py)

```powershell
uvicorn app:app --host 0.0.0.0 --port 8000 --reload
```

Verifique se subiu:

```powershell
Invoke-RestMethod http://localhost:8000/health
```

## 7. Apontar o webhook da Evolution API para a ponte

Como a Evolution API roda em container Docker e `app.py` roda no host, use
`host.docker.internal` (Docker Desktop no Windows) para o container alcancar
a porta 8000 do host:

```powershell
Invoke-RestMethod -Method Post `
  -Uri "http://localhost:8080/webhook/set/my-instance" `
  -Headers @{ apikey = $env:GLOBAL_KEY } `
  -ContentType "application/json" `
  -Body (@{
      webhook = @{
          url            = "http://host.docker.internal:8000/webhook"
          enabled        = $true
          webhookByEvents = $false
          events         = @("MESSAGES_UPSERT")
      }
  } | ConvertTo-Json -Depth 5)
```

## 8. Testar

Envie uma mensagem de texto para o numero conectado no WhatsApp. O fluxo
esperado nos logs do `uvicorn`:

```
INFO:     127.0.0.1 - "POST /webhook HTTP/1.1" 200 OK
```

E a resposta do Gemini deve chegar de volta no WhatsApp.

## Encerrar

```powershell
# parar a ponte: Ctrl+C no terminal do uvicorn
deactivate                 # sair do venv
docker compose down        # parar Evolution API + Postgres + Redis
docker compose down -v     # idem, e apaga os volumes (instancia, DB, cache)
```

## Notas

- O historico de conversa por contato fica em memoria (`chat_histories` em
  `app.py`) e e perdido ao reiniciar o processo — nao ha persistencia em
  disco/DB para o historico.
- Os endpoints exatos da Evolution API (`/instance/create`, `/webhook/set`,
  `/message/sendText`) podem variar entre builds/versoes; confira a versao
  fixada em `docker-compose.yml` (`atendai/evolution-api:v2.2.0`) caso algum
  endpoint retorne 404.
