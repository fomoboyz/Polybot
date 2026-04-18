"""Durable state (SQLite). Records placed orders, fills, and running PnL so
the bot can restart without losing accounting."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from sqlalchemy import (
    Column,
    Float,
    Integer,
    String,
    create_engine,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Session

log = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


class OrderRow(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(String, unique=True, nullable=False, index=True)
    token_id = Column(String, nullable=False, index=True)
    side = Column(String, nullable=False)
    price = Column(Float, nullable=False)
    size = Column(Float, nullable=False)
    created_at = Column(Float, nullable=False)
    status = Column(String, nullable=False, default="open")


class FillRow(Base):
    __tablename__ = "fills"
    id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(String, nullable=False, index=True)
    token_id = Column(String, nullable=False, index=True)
    side = Column(String, nullable=False)
    price = Column(Float, nullable=False)
    shares = Column(Float, nullable=False)
    filled_at = Column(Float, nullable=False)


class MetricRow(Base):
    __tablename__ = "metrics"
    id = Column(Integer, primary_key=True, autoincrement=True)
    ts = Column(Float, nullable=False, index=True)
    name = Column(String, nullable=False, index=True)
    value = Column(Float, nullable=False)


class Store:
    def __init__(self, db_path: str = "state/polybot.sqlite"):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(self._engine)

    def record_order(self, order_id: str, token_id: str, side: str, price: float, size: float, created_at: float) -> None:
        with Session(self._engine) as s:
            s.add(OrderRow(
                order_id=order_id, token_id=token_id, side=side,
                price=price, size=size, created_at=created_at,
            ))
            s.commit()

    def mark_order(self, order_id: str, status: str) -> None:
        with Session(self._engine) as s:
            row = s.scalar(select(OrderRow).where(OrderRow.order_id == order_id))
            if row:
                row.status = status
                s.commit()

    def record_fill(self, order_id: str, token_id: str, side: str, price: float, shares: float, filled_at: float) -> None:
        with Session(self._engine) as s:
            s.add(FillRow(
                order_id=order_id, token_id=token_id, side=side,
                price=price, shares=shares, filled_at=filled_at,
            ))
            s.commit()

    def record_metric(self, ts: float, name: str, value: float) -> None:
        with Session(self._engine) as s:
            s.add(MetricRow(ts=ts, name=name, value=value))
            s.commit()
