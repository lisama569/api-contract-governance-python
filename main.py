
from app.api import create_app
from app.logging_setup import configure_logging

configure_logging()

app = create_app()

@app.get("/")
def read_root() -> dict[str, str]:
    return {"service": "api-contract-governance", "status": "ok"}
