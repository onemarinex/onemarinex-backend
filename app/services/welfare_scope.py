"""Shared recorded country filter. Unknown geography is never guessed."""

from fastapi import HTTPException
from sqlalchemy import select, or_
from app.db.models.port_geography import PortGeography


def filter_country(query, port_column, country_code=None):
    if not country_code:
        return query
    if country_code == "UNKNOWN":
        return query.filter(
            or_(port_column.is_(None), ~port_column.in_(select(PortGeography.port_id)))
        )
    if (
        len(country_code) != 2
        or not country_code.isascii()
        or not country_code.isalpha()
        or not country_code.isupper()
    ):
        raise HTTPException(422, "Use a recorded two-letter country code or UNKNOWN.")
    return query.filter(
        port_column.in_(
            select(PortGeography.port_id).where(
                PortGeography.country_code == country_code
            )
        )
    )
