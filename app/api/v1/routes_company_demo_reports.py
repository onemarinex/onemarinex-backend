"""Authenticated, read-only fictional PDF reports. No database writes or emails."""
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field, ValidationError
from jose import jwt, JWTError
from app.core.config import settings
from app.api.v1.routes_auth import get_current_user
from app.api.v1.routes_shipping_company import current_company_member
from app.services.company_demo_fixture import FIXTURE_VERSION, month_start
from app.services import company_demo_reports as demo
from app.services.company_pdf import render_report

router = APIRouter()
TOKEN_TYPE = "company_demo_report"
TOKEN_MINUTES = 30


class DemoReportIn(BaseModel):
    model_config = {"extra": "forbid"}
    report_type: Literal["EXECUTIVE", "FLEET", "PORT", "CALL", "WELFARE", "SAFETY"]
    start: datetime
    end: datetime
    vessel_id: Optional[int] = None
    port_id: Optional[int] = None
    call_id: Optional[int] = None
    country_code: Optional[str] = Field(default=None, max_length=7)
    configuration: Optional[dict] = None


class DemoRenderIn(BaseModel):
    model_config = {"extra": "forbid"}
    token: str = Field(min_length=20, max_length=12000)


def now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def issue_preview(body, *, actor_id, company_id, anchor):
    params = body.model_dump(mode="json")
    payload = demo.prepare(params, anchor)
    expires = anchor + timedelta(minutes=TOKEN_MINUTES)
    token = jwt.encode({
        "type": TOKEN_TYPE, "actor_id": actor_id, "company_id": company_id,
        "fixture_version": FIXTURE_VERSION, "anchor": anchor.isoformat(),
        "params": params, "iat": int(anchor.timestamp()), "exp": int(expires.timestamp()),
    }, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    return {"token": token, "payload": payload, "expires_at": expires.isoformat()}


def restore_preview(token, *, actor_id, company_id):
    try:
        claims = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM], options={"require_exp": True, "require_iat": True})
        if claims.get("type") != TOKEN_TYPE or claims.get("fixture_version") != FIXTURE_VERSION or claims.get("actor_id") != actor_id or claims.get("company_id") != company_id:
            raise ValueError("Invalid report context")
        anchor = datetime.fromisoformat(claims["anchor"])
        if anchor.tzinfo is None:
            raise ValueError("Invalid report timestamp")
        body = DemoReportIn(**claims["params"])
        return demo.prepare(body.model_dump(mode="json"), anchor)
    except (JWTError, ValueError, TypeError, KeyError, ValidationError):
        raise HTTPException(403, "The demonstration preview is expired or invalid. Generate a new preview.") from None


@router.get("/demo/options")
def options(response: Response, context=Depends(current_company_member)):
    response.headers["Cache-Control"] = "no-store"
    return demo.options(now())


@router.get("/demo/calls")
def calls(
    response: Response,
    start: Optional[datetime] = None, end: Optional[datetime] = None,
    country_code: Optional[str] = Query(None, max_length=7),
    port_id: Optional[int] = None, vessel_id: Optional[int] = None,
    search: str = Query("", max_length=120), offset: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=100), context=Depends(current_company_member),
):
    response.headers["Cache-Control"] = "no-store"
    anchor = now()
    if (start is None) != (end is None):
        raise HTTPException(422, "Supply both period boundaries.")
    params = {"start": start or month_start(anchor, -2), "end": end or month_start(anchor, 1), "country_code": country_code, "port_id": port_id, "vessel_id": vessel_id}
    try:
        return demo.call_page(anchor, params, search, offset, limit)
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from None


@router.post("/demo/preview")
def preview(body: DemoReportIn, response: Response, context=Depends(current_company_member), actor=Depends(get_current_user)):
    response.headers["Cache-Control"] = "no-store"
    try:
        return issue_preview(body, actor_id=actor.id, company_id=context[0].id, anchor=now())
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from None


@router.post("/demo/render")
def render(body: DemoRenderIn, context=Depends(current_company_member), actor=Depends(get_current_user)):
    payload = restore_preview(body.token, actor_id=actor.id, company_id=context[0].id)
    return Response(render_report(payload), media_type="application/pdf", headers={
        "Content-Disposition": 'inline; filename="company-fictional-report.pdf"',
        "Cache-Control": "no-store",
    })


@router.get("/demo/branding")
def demo_branding(response: Response, context=Depends(current_company_member)):
    from app.services.company_demo_branding import branding
    response.headers["Cache-Control"] = "no-store"
    return branding()
