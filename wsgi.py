"""
WSGI entry point para Heroku / gunicorn.
Inicia os threads do Polymarket antes de servir requisições.
"""
import os
import threading
from app import app, sio, load_config
import polymarket_live as pm_live


def _start_background():
    from app import state_broadcaster
    threading.Thread(target=state_broadcaster, daemon=True).start()
    cfg = load_config()
    pm_key = cfg.get("pm_private_key", "")
    pm_live.start_pm_live(sio, pm_key)


_start_background()
