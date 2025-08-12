# Strava + GPT Action: Backend listo para desplegar

## Requisitos
- Python 3.10+
- Cuenta Strava (gratis)
- Una URL pública (Render/ Railway).

## Uso local (opcional)
```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```
Abre http://localhost:8000/health

## Variables de entorno (.env)
```
STRAVA_CLIENT_ID=12345
STRAVA_CLIENT_SECRET=xxxxxxxx
BASE_URL=https://tu-servicio.onrender.com
ADMIN_TOKEN=pon-una-clave-segura
```
