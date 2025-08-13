from sqlalchemy import create_engine, select, and_
from sqlalchemy.orm import sessionmaker
from .models import Base, Token, Activity
from .config import DATABASE_URL

# Crea el engine con sane defaults para Postgres y SQLite
connect_args = {}
if DATABASE_URL.startswith("sqlite"):
    # Evita problemas de threads en SQLite local
    connect_args = {"check_same_thread": False}

engine = create_engine(
    DATABASE_URL,
    future=True,
    pool_pre_ping=True,
    connect_args=connect_args
)

SessionLocal = sessionmaker(engine, expire_on_commit=False, future=True)

# Inicializa tablas
Base.metadata.create_all(engine)


def upsert_token(athlete_id: int, access_token: str, refresh_token: str, expires_at: int):
    with SessionLocal() as s:
        t = s.execute(select(Token).where(Token.athlete_id == athlete_id)).scalar_one_or_none()
        if t:
            t.access_token = access_token
            t.refresh_token = refresh_token
            t.expires_at = expires_at
        else:
            t = Token(athlete_id=athlete_id, access_token=access_token, refresh_token=refresh_token, expires_at=expires_at)
            s.add(t)
        s.commit()

def get_token(athlete_id: int) -> Token | None:
    with SessionLocal() as s:
        return s.execute(select(Token).where(Token.athlete_id == athlete_id)).scalar_one_or_none()

def save_or_update_activity(act: dict, athlete_id: int):
    from datetime import datetime
    with SessionLocal() as s:
        existing = s.get(Activity, act["id"])
        if not existing:
            db = Activity(
                id=act["id"],
                athlete_id=athlete_id,
                type=act.get("type",""),
                start_date=datetime.fromisoformat(act["start_date"][:-1]) if act.get("start_date") else datetime.utcnow(),
                name=act.get("name",""),
                distance_m=int(act.get("distance",0)),
                moving_time_s=int(act.get("moving_time",0)),
                elapsed_time_s=int(act.get("elapsed_time",0)),
                total_elevation_gain_m=int(act.get("total_elevation_gain",0)),
                average_heartrate=int(act.get("average_heartrate") or 0) if act.get("average_heartrate") else None,
                max_heartrate=int(act.get("max_heartrate") or 0) if act.get("max_heartrate") else None,
                raw=act
            )
            s.add(db)
        else:
            existing.name = act.get("name", existing.name)
            existing.distance_m = int(act.get("distance", existing.distance_m))
            existing.moving_time_s = int(act.get("moving_time", existing.moving_time_s))
            existing.elapsed_time_s = int(act.get("elapsed_time", existing.elapsed_time_s))
            existing.total_elevation_gain_m = int(act.get("total_elevation_gain", existing.total_elevation_gain_m))
            if act.get("average_heartrate") is not None:
                existing.average_heartrate = int(act.get("average_heartrate"))
            if act.get("max_heartrate") is not None:
                existing.max_heartrate = int(act.get("max_heartrate"))
            if act.get("start_date"):
                existing.start_date = datetime.fromisoformat(act["start_date"][:-1])
            existing.type = act.get("type", existing.type)
            existing.raw = act
        s.commit()

def query_runs(athlete_id: int, start_iso: str, end_iso: str) -> list[Activity]:
    from datetime import datetime
    start = datetime.fromisoformat(start_iso)
    end = datetime.fromisoformat(end_iso)
    with SessionLocal() as s:
        stmt = select(Activity).where(
            and_(Activity.athlete_id == athlete_id,
                 Activity.type == "Run",
                 Activity.start_date >= start,
                 Activity.start_date <= end)
        ).order_by(Activity.start_date.asc())
        return list(s.execute(stmt).scalars().all())
def get_any_athlete_id() -> int | None:
    from .models import Token
    with SessionLocal() as s:
        t = s.execute(select(Token)).scalars().first()
        return t.athlete_id if t else None
