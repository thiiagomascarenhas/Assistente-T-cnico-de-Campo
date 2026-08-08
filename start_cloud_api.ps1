Set-Location "C:\Users\Thiago\whatsapp-gemini-bridge"
& ".\venv\Scripts\python.exe" -m uvicorn app_cloud_api:app --host 0.0.0.0 --port 8001 *>> ".\cloud_api.log"
