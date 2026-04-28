from __future__ import annotations

import uvicorn

from meshonator.api.app import app
from meshonator.config.settings import get_settings

settings = get_settings()


if __name__ == "__main__":
    uvicorn.run("meshonator.api.app:app", host=settings.app_host, port=settings.app_port, reload=settings.app_env == "dev")
