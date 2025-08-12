from datetime import datetime
from sqlalchemy.orm import declarative_base, Mapped, mapped_column
from sqlalchemy import String, Integer, DateTime, JSON

Base = declarative_base()

class Token(Base):
    __tablename__ = "tokens"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    athlete_id: Mapped[int] = mapped_column(Integer, index=True, unique=True)
    access_token: Mapped[str] = mapped_column(String)
    refresh_token: Mapped[str] = mapped_column(String)
    expires_at: Mapped[int] = mapped_column(Integer)  # epoch seconds

class Activity(Base):
    __tablename__ = "activities"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # Strava activity id
    athlete_id: Mapped[int] = mapped_column(Integer, index=True)
    type: Mapped[str] = mapped_column(String, index=True)
    start_date: Mapped[datetime] = mapped_column(DateTime, index=True)  # <- aquí el fix
    name: Mapped[str] = mapped_column(String)
    distance_m: Mapped[int] = mapped_column(Integer)
    moving_time_s: Mapped[int] = mapped_column(Integer)
    elapsed_time_s: Mapped[int] = mapped_column(Integer)
    total_elevation_gain_m: Mapped[int] = mapped_column(Integer)
    average_heartrate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_heartrate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw: Mapped[dict] = mapped_column(JSON)

