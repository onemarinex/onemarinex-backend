"""Read-only four-state legacy origin counts; prints no records or PII.

Uses the explicitly configured DATABASE_URL. Run only against an authorized
read-only database connection. This command never reconciles or backfills rows.
"""
from sqlalchemy import func, text
from app.db.session import SessionLocal
from app.db.models.cab_booking import CabBooking
from app.db.models.incident import Incident
from app.db.models.crew_sos import CrewSos
from app.services.event_origin import origin_expression


def main():
    with SessionLocal() as db:
        db.execute(text('SET TRANSACTION READ ONLY'))
        for model in (CabBooking, Incident, CrewSos):
            state = origin_expression(model)
            counts = dict(db.query(state, func.count(model.id)).group_by(state).all())
            print(model.__tablename__, counts)
        db.rollback()


if __name__ == '__main__':
    main()
