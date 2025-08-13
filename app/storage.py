# app/storage.py
from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Integer,
    String,
    create_engine,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import declarative_base, sessionmaker, Session

# Para parsear las fechas ISO que llegan desde Strava
from dateutil import parser as dateparser


# ------------------------------------------------------------------------------
# Configuración de SQLAlchemy
# ------------------------------------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("DB_URL")
if not DATABASE_URL:
    # No ponemos valor por defecto para evitar conectarnos mal sin querer.
    raise RuntimeError("DATABASE_URL no está configurada en las variables de entorno.")

# Con psycopg v3, el driver en el URL debe ser 'postgresql+psycopg://'
# Render/Neon suelen exigir SSL, ya va en tu URL (?sslmode=require).
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
Base = declarative_base()


# ------------------------------------------------------------------------------
# Modelos
# ------------------------------------------------------------------------------
class Activity(Base):
    __tablename__ = "activities"

    # OJO: BIGINT para soportar IDs grandes de Strava
    id = Column(BigInteger, primary_key=True)
    athlete_id = Column(BigInteger, index=True, nullable=False)

    type = Column(String(30), index=True, nullable=True)  # "Run", "TrailRun", etc.
    start_date = Column(DateTime(timezone=True), index=True, nullable=True)
    name = Column(String(255), nullable=True)

    distance_m = Column(Integer, nullable=True)             # metros
    moving_time_s = Column(Integer, nullable=True)          # segundos
    elapsed_time_s = Column(Integer, nullable=True)         # segundos
    total_elevation_gain_m = Column(Integer, nullable=True) # metros
    average_heartrate = Column(Integer, nullable=True)
    max_heartrate = Column(Integer, nullable=True)

    raw = Column(JSONB, nullable=True)  # payload completo por si hace falta


class Token(Base):
    __tablename__ = "tokens"

    # PK por atleta. BIGINT para soportar IDs de Strava.
    athlete_id = Column(BigInteger, primary_key=True)
    access_token = Column(String(255), nullable=False)
    refresh_token = Column(String(255), nullable=False)
    scope = Column(String(255), nullable=True)
    # expires_at en “epoch seconds” (lo habitual en Strava OAuth)
    expires_at = Column(Integer, nullable=False)


# ------------------------------------------------------------------------------
# Utilidades de DB
# ------------------------------------------------------------------------------
def init_db() -> None:
    """Crea tablas si no existen."""
    Base.metadata.create_all(bind=engine)


def get_db() -> Session:
    """Session de conveniencia (si usas dependencias FastAPI)."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def upgrade_ids_to_bigint(db_engine=None) -> None:
    """
    Cambia columnas claves a BIGINT (id, athlete_id) por si existen en INTEGER,
    para evitar 'integer out of range' con IDs grandes de Strava.
    Es idempotente: si ya están en BIGINT, las sentencias pueden fallar y se ignoran.
    """
    if db_engine is None:
        db_engine = engine

    stmts = [
        'ALTER TABLE "activities" ALTER COLUMN "id" TYPE BIGINT USING "id"::BIGINT;',
        'ALTER TABLE "activities" ALTER COLUMN "athlete_id" TYPE BIGINT USING "athlete_id"::BIGINT;',
        'ALTER TABLE "tokens" ALTER COLUMN "athlete_id" TYPE BIGINT USING "athlete_id"::BIGINT;',
    ]
    with db_engine.begin() as conn:
        for s in stmts:
            try:
                conn.exec_driver_sql(s)
            except Exception:
                # Si ya es BIGINT u otra causa no crítica, ignoramos.
                pass


# ------------------------------------------------------------------------------
# Tokens (OAuth)
# ------------------------------------------------------------------------------
def upsert_token(
    athlete_id: int,
    access_token: str,
    refresh_token: str,
    expires_at: datetime,
    scope: str | None = None,
) -> None:
    """Inserta/actualiza el token de un atleta."""
    db: Session = SessionLocal()
    try:
        tok = db.get(Token, athlete_id)
        if tok is None:
            tok = Token(
                athlete_id=athlete_id,
                access_token=access_token,
                refresh_token=refresh_token,
                scope=scope or "",
                expires_at=expires_at,
            )
            db.add(tok)
        else:
            tok.access_token = access_token
            tok.refresh_token = refresh_token
            tok.scope = scope or ""
            tok.expires_at = expires_at
        db.commit()
    finally:
        db.close()


def get_token(athlete_id: int) -> Optional[Token]:
    """Devuelve el token de un atleta o None si no existe."""
    db: Session = SessionLocal()
    try:
        return db.get(Token, athlete_id)
    finally:
        db.close()


# ------------------------------------------------------------------------------
# Activities
# ------------------------------------------------------------------------------
def _parse_start_date(payload: Dict[str, Any]) -> Optional[datetime]:
    # Strava suele enviar 'start_date' (UTC) y 'start_date_local'.
    for key in ("start_date", "start_date_local"):
        value = payload.get(key)
        if value:
            try:
                return dateparser.isoparse(value)
            except Exception:
                pass
    return None


def save_or_update_activity(activity_payload: Dict[str, Any]) -> None:
    """
    Guarda/actualiza una actividad de Strava.
    El payload es el objeto JSON de Strava (por ejemplo, recibido por webhook o por import inicial).
    """
    act_id = int(activity_payload.get("id"))
    # athlete puede venir como objeto {"id": ...} o como athlete_id en algunos contextos
    athlete_id = activity_payload.get("athlete", {}).get("id") or activity_payload.get("athlete_id")
    athlete_id = int(athlete_id) if athlete_id is not None else 0

    # Tipo: 'sport_type' (nuevo) o 'type' (antiguo)
    act_type = activity_payload.get("sport_type") or activity_payload.get("type") or None
    start_date = _parse_start_date(activity_payload)
    name = activity_payload.get("name")

    distance_m = int(round(activity_payload.get("distance") or 0))
    moving_time_s = int(activity_payload.get("moving_time") or 0)
    elapsed_time_s = int(activity_payload.get("elapsed_time") or 0)
    total_elevation_gain_m = int(round(activity_payload.get("total_elevation_gain") or 0))

    avg_hr = activity_payload.get("average_heartrate")
    max_hr = activity_payload.get("max_heartrate")
    average_heartrate = int(round(avg_hr)) if isinstance(avg_hr, (int, float)) else None
    max_heartrate = int(round(max_hr)) if isinstance(max_hr, (int, float)) else None

    db: Session = SessionLocal()
    try:
        existing = db.get(Activity, act_id)
        if existing is None:
            existing = Activity(
                id=act_id,
                athlete_id=athlete_id,
                type=act_type,
                start_date=start_date,
                name=name,
                distance_m=distance_m,
                moving_time_s=moving_time_s,
                elapsed_time_s=elapsed_time_s,
                total_elevation_gain_m=total_elevation_gain_m,
                average_heartrate=average_heartrate,
                max_heartrate=max_heartrate,
                raw=activity_payload,
            )
            db.add(existing)
        else:
            existing.athlete_id = athlete_id
            existing.type = act_type
            existing.start_date = start_date
            existing.name = name
            existing.distance_m = distance_m
            existing.moving_time_s = moving_time_s
            existing.elapsed_time_s = elapsed_time_s
            existing.total_elevation_gain_m = total_elevation_gain_m
            existing.average_heartrate = average_heartrate
            existing.max_heartrate = max_heartrate
            existing.raw = activity_payload

        db.commit()
    finally:
        db.close()


def query_runs(athlete_id: int, start: datetime, end: datetime) -> List[Dict[str, Any]]:
    """
    Devuelve actividades de carrera (Run/Running/TrailRun…) para un atleta y rango [start, end].
    Retorna una lista de diccionarios con los campos útiles para el API.
    """
    RUN_TYPES = {"Run", "Running", "TrailRun", "VirtualRun"}

    db: Session = SessionLocal()
    try:
        stmt = (
            select(Activity)
            .where(Activity.athlete_id == athlete_id)
            .where(Activity.start_date >= start, Activity.start_date <= end)
            .where(Activity.type.in_(list(RUN_TYPES)))
            .order_by(Activity.start_date.asc())
        )
        rows = db.execute(stmt).scalars().all()

        out: List[Dict[str, Any]] = []
        for a in rows:
            # cálculos derivados
            distance_km = (a.distance_m or 0) / 1000.0
            moving_min = (a.moving_time_s or 0) / 60.0
            # ritmo (min/km)
            avg_pace = None
            if distance_km > 0 and moving_min > 0:
                pace_min = moving_min / distance_km
                # formato mm:ss
                mins = int(pace_min)
                secs = int(round((pace_min - mins) * 60))
                avg_pace = f"{mins}:{secs:02d} min/km"

            out.append(
                {
                    "id": int(a.id),
                    "date": a.start_date.isoformat() if a.start_date else None,
                    "name": a.name,
                    "distance_km": round(distance_km, 2),
                    "moving_time_min": round(moving_min, 2),
                    "avg_hr": a.average_heartrate,
                    "elev_gain_m": a.total_elevation_gain_m or 0,
                    "avg_pace": avg_pace,
                }
            )
        return out
    finally:
        db.close()
