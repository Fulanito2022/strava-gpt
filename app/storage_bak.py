import os
from typing import Optional, Dict, Any
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.orm import declarative_base, sessionmaker, Session

# --- Config DB --------------------------------------------------------------

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("Falta la variable de entorno DATABASE_URL")

# Normaliza el URI para psycopg3 si viene en formato 'postgres://'
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)

engine = sa.create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()

# --- Modelos ----------------------------------------------------------------


class Token(Base):
    __tablename__ = "tokens"

    athlete_id = sa.Column(sa.BigInteger, primary_key=True)
    access_token = sa.Column(sa.String, nullable=False)
    refresh_token = sa.Column(sa.String, nullable=False)
    # Importante: timestamptz
    expires_at = sa.Column(sa.DateTime(timezone=True), nullable=False, index=True)
    scope = sa.Column(sa.String, nullable=False, default="")


class Activity(Base):
    __tablename__ = "activities"

    id = sa.Column(sa.BigInteger, primary_key=True)
    athlete_id = sa.Column(sa.BigInteger, nullable=False, index=True)
    type = sa.Column(sa.String, nullable=False)
    name = sa.Column(sa.String, nullable=False)
    start_date = sa.Column(sa.DateTime(timezone=True), nullable=False, index=True)

    distance_m = sa.Column(sa.Integer, nullable=False)
    moving_time_s = sa.Column(sa.Integer, nullable=False)
    elapsed_time_s = sa.Column(sa.Integer, nullable=False)
    total_elevation_gain_m = sa.Column(sa.Integer)

    average_heartrate = sa.Column(sa.Float)
    max_heartrate = sa.Column(sa.Float)

    # JSON (Postgres lo mapea a JSON/JSONB según el dialecto)
    raw = sa.Column(sa.JSON, nullable=False)


# Crea las tablas si no existen (no migra tipos existentes)
Base.metadata.create_all(bind=engine)


# --- Helpers ----------------------------------------------------------------

def get_db():
    """Dependency de FastAPI: yield Session y cierra al final."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _to_utc_datetime(value) -> datetime:
    """Convierte epoch/int/str/datetime a datetime con tz=UTC."""
    if value is None:
        raise ValueError("datetime requerido")
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str):
        # Soporta 'Z'
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    raise TypeError(f"Tipo no soportado para fecha: {type(value)}")


# --- API de acceso ----------------------------------------------------------

def get_token(athlete_id: int) -> Optional[Token]:
    with SessionLocal() as db:
        return db.get(Token, athlete_id)


def get_any_athlete_id() -> Optional[int]:
    with SessionLocal() as db:
        return db.query(Token.athlete_id).order_by(Token.athlete_id.asc()).limit(1).scalar()


def upsert_token(
    *,
    athlete_id: int,
    access_token: str,
    refresh_token: str,
    expires_at,  # puede venir como epoch/int o datetime/str
    scope: str = "",
) -> None:
    exp_dt = _to_utc_datetime(expires_at)

    with SessionLocal() as db:
        tok = db.get(Token, athlete_id)
        if tok:
            tok.access_token = access_token
            tok.refresh_token = refresh_token
            tok.expires_at = exp_dt
            tok.scope = scope or ""
        else:
            tok = Token(
                athlete_id=athlete_id,
                access_token=access_token,
                refresh_token=refresh_token,
                expires_at=exp_dt,
                scope=scope or "",
            )
            db.add(tok)
        db.commit()


def save_or_update_activity(act: Dict[str, Any], db: Optional[Session] = None) -> None:
    """
    Guarda/actualiza una actividad Strava. Espera el dict crudo de la API.
    - raw se guarda como JSON (no string).
    - start_date se guarda en UTC.
    """
    close_session = False
    if db is None:
        db = SessionLocal()
        close_session = True

    try:
        activity_id = int(act["id"])
        athlete_id = int(
            (act.get("athlete") or {}).get("id") or act.get("athlete_id") or 0
        )

        start_iso = act.get("start_date") or act.get("start_date_local")
        start_dt = _to_utc_datetime(start_iso)

        distance_m = int(round(float(act.get("distance") or 0)))
        moving_time_s = int(act.get("moving_time") or 0)
        elapsed_time_s = int(act.get("elapsed_time") or 0)
        elev_m = act.get("total_elevation_gain")
        total_elevation_gain_m = int(round(float(elev_m))) if elev_m is not None else None

        average_heartrate = (
            float(act.get("average_heartrate")) if act.get("average_heartrate") is not None else None
        )
        max_heartrate = (
            float(act.get("max_heartrate")) if act.get("max_heartrate") is not None else None
        )

        name = (act.get("name") or "").strip()
        typ = (act.get("type") or "").strip() or "Workout"

        existing = db.get(Activity, activity_id)
        if existing:
            existing.athlete_id = athlete_id
            existing.type = typ
            existing.name = name
            existing.start_date = start_dt
            existing.distance_m = distance_m
            existing.moving_time_s = moving_time_s
            existing.elapsed_time_s = elapsed_time_s
            existing.total_elevation_gain_m = total_elevation_gain_m
            existing.average_heartrate = average_heartrate
            existing.max_heartrate = max_heartrate
            existing.raw = act  # dict -> JSON
        else:
            db.add(
                Activity(
                    id=activity_id,
                    athlete_id=athlete_id,
                    type=typ,
                    name=name,
                    start_date=start_dt,
                    distance_m=distance_m,
                    moving_time_s=moving_time_s,
                    elapsed_time_s=elapsed_time_s,
                    total_elevation_gain_m=total_elevation_gain_m,
                    average_heartrate=average_heartrate,
                    max_heartrate=max_heartrate,
                    raw=act,  # dict -> JSON
                )
            )
        db.commit()
    finally:
        if close_session:
            db.close()
