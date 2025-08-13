import os
from datetime import datetime, timezone
from typing import Optional, Dict, Any

from sqlalchemy import (
    create_engine, Column, BigInteger, String, Integer, Float,
    DateTime, JSON, select
)
from sqlalchemy.orm import sessionmaker, declarative_base, Session

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL no está definido")

engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()


class Token(Base):
    __tablename__ = "tokens"
    athlete_id = Column(BigInteger, primary_key=True)
    access_token = Column(String, nullable=False)
    refresh_token = Column(String, nullable=False)
    scope = Column(String, nullable=True)
    # La columna en Postgres es TIMESTAMP WITH TIME ZONE
    expires_at = Column(DateTime(timezone=True), nullable=False)


class Activity(Base):
    __tablename__ = "activities"
    id = Column(BigInteger, primary_key=True)
    athlete_id = Column(BigInteger, index=True, nullable=False)
    type = Column(String, nullable=True)
    name = Column(String, nullable=True)
    start_date = Column(DateTime(timezone=True), nullable=False)
    distance_m = Column(Integer, nullable=True)
    moving_time_s = Column(Integer, nullable=True)
    elapsed_time_s = Column(Integer, nullable=True)
    total_elevation_gain_m = Column(Integer, nullable=True)
    average_heartrate = Column(Float, nullable=True)
    max_heartrate = Column(Float, nullable=True)
    # IMPORTANTE: JSON (no String) para que no lo castee a VARCHAR
    raw = Column(JSON, nullable=False)


def get_db() -> Session:
    return SessionLocal()


def get_any_athlete_id(db: Optional[Session] = None) -> Optional[int]:
    close_after = False
    if db is None:
        db = get_db()
        close_after = True
    try:
        res = db.execute(select(Token.athlete_id).limit(1)).first()
        return res[0] if res else None
    finally:
        if close_after:
            db.close()


def get_token(athlete_id: int, db: Optional[Session] = None) -> Optional[Token]:
    close_after = False
    if db is None:
        db = get_db()
        close_after = True
    try:
        stmt = select(Token).where(Token.athlete_id == athlete_id).limit(1)
        row = db.execute(stmt).scalar_one_or_none()
        return row
    finally:
        if close_after:
            db.close()


def upsert_token(
    *,
    athlete_id: int,
    access_token: str,
    refresh_token: str,
    expires_at,  # puede venir como epoch(int) o datetime
    scope: Optional[str] = None,
    db: Optional[Session] = None,
) -> None:
    close_after = False
    if db is None:
        db = get_db()
        close_after = True

    # Normaliza expires_at a datetime con tz
    if isinstance(expires_at, (int, float)):
        expires_dt = datetime.fromtimestamp(int(expires_at), tz=timezone.utc)
    elif isinstance(expires_at, datetime):
        expires_dt = expires_at.astimezone(timezone.utc)
    else:
        raise ValueError("expires_at debe ser int(epoch) o datetime")

    try:
        existing = db.get(Token, athlete_id)
        if existing:
            existing.access_token = access_token
            existing.refresh_token = refresh_token
            existing.expires_at = expires_dt
            existing.scope = scope or existing.scope
        else:
            db.add(
                Token(
                    athlete_id=athlete_id,
                    access_token=access_token,
                    refresh_token=refresh_token,
                    expires_at=expires_dt,
                    scope=scope or "",
                )
            )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        if close_after:
            db.close()


def save_or_update_activity(activity: Dict[str, Any], db: Optional[Session] = None) -> None:
    """
    Recibe el dict que devuelve Strava y lo persiste.
    `raw` se guarda como JSON (dict), NO como string.
    """
    from dateutil import parser as dtparser

    close_after = False
    if db is None:
        db = get_db()
        close_after = True

    try:
        act_id = int(activity["id"])
        # start_date de Strava es ISO con Z -> tz-aware
        start_dt = dtparser.parse(activity.get("start_date"))  # p.ej. "2024-09-27T17:23:20Z"

        obj = db.get(Activity, act_id)
        payload = {
            "id": act_id,
            "athlete_id": int(activity.get("athlete", {}).get("id") or activity.get("athlete_id")),
            "type": activity.get("sport_type") or activity.get("type"),
            "name": activity.get("name"),
            "start_date": start_dt,
            "distance_m": int(activity.get("distance", 0)) if activity.get("distance") is not None else None,
            "moving_time_s": int(activity.get("moving_time", 0)) if activity.get("moving_time") is not None else None,
            "elapsed_time_s": int(activity.get("elapsed_time", 0)) if activity.get("elapsed_time") is not None else None,
            "total_elevation_gain_m": int(activity.get("total_elevation_gain", 0)) if activity.get("total_elevation_gain") is not None else None,
            "average_heartrate": float(activity.get("average_heartrate")) if activity.get("average_heartrate") is not None else None,
            "max_heartrate": float(activity.get("max_heartrate")) if activity.get("max_heartrate") is not None else None,
            "raw": activity,  # <-- dict directo, SQLAlchemy lo serializa a JSON
        }

        if obj:
            for k, v in payload.items():
                setattr(obj, k, v)
        else:
            obj = Activity(**payload)
            db.add(obj)

        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        if close_after:
            db.close()
