"""
The declarative base -- the single class every database model inherits from.

Inheriting from Base is what tells SQLAlchemy "this class describes a
table." Base also collects every model's table definition into
Base.metadata, which is how Alembic knows what the schema is supposed to
look like when it generates migrations.

This lives in its own tiny module (rather than inside models.py) so that
Alembic can import Base without importing anything else, avoiding
circular imports later.
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
