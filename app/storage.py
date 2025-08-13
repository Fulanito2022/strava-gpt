import os
import json
from datetime import datetime, timezone, date
from typing import Optional, List

from sqlalchemy import (
    create_engine,
    BigInteger,
    String,
    Text,
    DateTime,
    Integer,
    Float,
    select,
    and_,
    or_,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, Session
from dateutil.parser import isoparse

# DATABASE_URL debe ser del estilo:
# postgresql+psycopg://USER:PASS@HOST/DB?sslmode=require&channel_binding=require
DATABASE_URL = (
    os.getenv("DATABASE_URL")
    or os.getenv("DB_URL")
    or os.getenv("DATABASE_URL".upper())
)

if not DATABASE_URL:
    raise RuntimeError("Falta DATABASE_URL en variables de entorno")

engine = create_engine(DATABASE_URL, pool_pre_ping=True)


class Base(DeclarativeBase):
    pass


class Token(Base):
    __tablename__ = "tokens"

    # PK = athlete_id (de Strava). ¡Ojo! Es BIGINT.
    athlete_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    access_token: Mapped[str] = mapped_column(String, nullable=False)
    refresh_token: Mapped[str] = mapped_column(String, nullable=False)
    # Guardamos como timestamptz en BD
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Strava devuelve el scope como string "read,activity:read_all,profile:read_all"
    scope: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class Activity(Base):
    __tablename__ = "activities"

    # IDs de Strava son grandes -> BIGINT
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    athlete_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # sport/type y nombre
    type: Mapped[str] = mapped_column(String(50), nullable=False)  # "Run", "TrailRun", etc.
    name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # fechas y métricas
    start_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    distance_m: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    moving_time_s: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    elapsed_time_s: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_elevation_gain_m: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    average_heartrate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    max_heartrate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # crudo (JSON) como texto
    raw: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


# Crear tablas si no existen
Base.metadata.create_all(engine)


def _to_datetime_tz(value) -> datetime:
    """Convierte epoch (int/float) o datetime a datetime con tz=UTC."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    # value epoch en segundos
    return datetime.fromtimestamp(float(value), tz=timezone.utc)


def upsert_token(
    athlete_id: int,
    access_token: str,
    refresh_token: str,
    expires_at,  # int epoch o datetime
    scope: Optional[str] = None,
) -> None:
    exp_dt = _to_datetime_tz(expires_at)
    with Session(engine) as db:
        tok = db.get(Token, athlete_id)
        if tok:
            tok.access_token = access_token
            tok.refresh_token = refresh_token
            tok.expires_at = exp_dt
            tok.scope = scope
        else:
            db.add(
                Token(
                    athlete_id=athlete_id,
                    access_token=access_token,
                    refresh_token=refresh_token,
                    expires_at=exp_dt,
                    scope=scope,
                )
            )
        db.commit()


def get_token(athlete_id: int) -> Optional[Token]:
    with Session(engine) as db:
        return db.get(Token, athlete_id)


def list_tokens() -> List[Token]:
    with Session(engine) as db:
        return list(db.scalars(select(Token)).all())


def save_or_update_activity(act: dict) -> None:
    """Guarda/actualiza una actividad de Strava."""
    # Campos robustos (Strava usa sport_type en endpoints nuevos)
    act_id = int(act["id"])
    athlete_id = int(
        act.get("athlete", {}).get("id") or act.get("athlete_id") or 0
    )
    sport = act.get("sport_type") or act.get("type") or "Other"
    name = act.get("name")
    start_date_str = act.get("start_date") or act.get("start_date_local")
    start_dt = isoparse(start_date_str) if start_date_str else datetime.now(timezone.utc)

    distance_m = int(act.get("distance") or 0)
    moving_time_s = int(act.get("moving_time") or 0)
    elapsed_time_s = int(act.get("elapsed_time") or 0)
    elev = int(round(float(act.get("total_elevation_gain") or 0)))

    avg_hr = act.get("average_heartrate")
    max_hr = act.get("max_heartrate")

    raw_txt = json.dumps(act, ensure_ascii=False)

    with Session(engine) as db:
        row = db.get(Activity, act_id)
        if row:
            row.athlete_id = athlete_id or row.athlete_id
            row.type = sport
            row.name = name
            row.start_date = start_dt
            row.distance_m = distance_m
            row.moving_time_s = moving_time_s
            row.elapsed_time_s = elapsed_time_s
            row.total_elevation_gain_m = elev
            row.average_heartrate = avg_hr
            row.max_heartrate = max_hr
            row.raw = raw_txt
        else:
            db.add(
                Activity(
                    id=act_id,
                    athlete_id=athlete_id,
                    type=sport,
                    name=name,
                    start_date=start_dt,
                    distance_m=distance_m,
                    moving_time_s=moving_time_s,
                    elapsed_time_s=elapsed_time_s,
                    total_elevation_gain_m=elev,
                    average_heartrate=avg_hr,
                    max_heartrate=max_hr,
                    raw=raw_txt,
                )
            )
        db.commit()


def query_runs(start: date, end: date) -> List[Activity]:
    """Devuelve actividades de tipo running en [start, end] (ambos inclusive)."""
    start_dt = datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc)
    # sumar casi un día para incluir todo 'end'
    end_dt = datetime.combine(end, datetime.max.time(), tzinfo=timezone.utc)

    run_types = ("Run", "TrailRun")  # sport_type/type
    with Session(engine) as db:
        stmt = (
            select(Activity)
            .where(
                and_(
                    Activity.start_date >= start_dt,
                    Activity.start_date <= end_dt,
                    or_(Activity.type.in_(run_types), Activity.type.ilike("%Run%")),
                )
            )
            .order_by(Activity.start_date.asc())
        )
        return list(db.scalars(stmt).all())
