import os
from sqlmodel import SQLModel, Field, create_engine, Session, select
from datetime import datetime
from typing import Optional, List
from dotenv import load_dotenv

load_dotenv()

class Fact(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    text: str = Field(unique=True)
    image_prompt: str
    image_url: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    posted: bool = Field(default=False)


class Channel(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    chat_id: str = Field(unique=True)  # @username или числовой ID
    added_at: datetime = Field(default_factory=datetime.utcnow)


# Читаем DATABASE_URL из окружения, по умолчанию используем SQLite
database_url = os.getenv("DATABASE_URL", "sqlite:///facts.db")

# Для PostgreSQL на некоторых платформах (например, Render) нужно заменить postgres:// на postgresql://
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

engine = create_engine(database_url)


def create_db_and_tables():
    SQLModel.metadata.create_all(engine)


def is_fact_duplicate(text: str) -> bool:
    with Session(engine) as session:
        statement = select(Fact).where(Fact.text == text)
        result = session.exec(statement).first()
        return result is not None


def save_fact(fact: Fact):
    with Session(engine) as session:
        session.add(fact)
        session.commit()
        session.refresh(fact)
        return fact


# Функции для каналов
def add_channel_to_db(chat_id: str):
    with Session(engine) as session:
        # Проверяем нет ли уже такого
        statement = select(Channel).where(Channel.chat_id == chat_id)
        if session.exec(statement).first():
            return False
        session.add(Channel(chat_id=chat_id))
        session.commit()
        return True


def get_all_channels() -> List[str]:
    with Session(engine) as session:
        statement = select(Channel)
        results = session.exec(statement).all()
        return [c.chat_id for c in results]


def remove_channel_from_db(chat_id: str):
    with Session(engine) as session:
        statement = select(Channel).where(Channel.chat_id == chat_id)
        channel = session.exec(statement).first()
        if channel:
            session.delete(channel)
            session.commit()
            return True
        return False
