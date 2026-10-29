"""Approved repository asset only. Never fetch a caller-provided logo URL."""
import base64
from pathlib import Path

def branding():
    asset = Path(__file__).resolve().parents[1] / "assets" / "onemarinex-logo.png"
    logo = None
    if asset.is_file():
        from app.api.v1.routes_company_administration import clean_logo
        logo = "data:image/png;base64," + base64.b64encode(clean_logo(base64.b64encode(asset.read_bytes()).decode())).decode()
    return {"name":"OneMarineX", "logo":logo}
