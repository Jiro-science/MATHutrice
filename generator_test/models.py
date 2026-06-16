from sqlmodel import SQLModel, Field, Relationship
from typing import Optional, List
from datetime import datetime
from decimal import Decimal
from uuid import UUID
from sqlalchemy import UniqueConstraint, Index


class Generer(SQLModel, table=True):
    __tablename__ = "generer"

    competence_id: UUID = Field(
        foreign_key="competence.competence_id",
        primary_key=True,
    )
    exercice_id: UUID = Field(
        foreign_key="exercice.exercice_id",
        primary_key=True,
    )


class User(SQLModel, table=True):
    __tablename__ = "user_"

    sso_id: UUID = Field(primary_key=True)
    created_at: datetime
    last_active: Optional[datetime] = None
    role: str = Field(max_length=50)
    email: str = Field(unique=True, max_length=100)
    name: str = Field(max_length=100)

    conversations: List["Conversation"] = Relationship(back_populates="user")
    progressions: List["Progression"] = Relationship(back_populates="user")
    recommendations: List["ModuleRecommendation"] = Relationship(back_populates="user")
    notion_progresses: List["NotionProgress"] = Relationship(back_populates="user")
    session_histories: List["SessionHistory"] = Relationship(back_populates="user")


class Notion(SQLModel, table=True):
    __tablename__ = "notion"

    notion_id: UUID = Field(primary_key=True)
    referentiel_key: str = Field(max_length=100, unique=True)
    title: str = Field(max_length=100)
    description: str

    competences: List["Competence"] = Relationship(back_populates="notion")
    conversations: List["Conversation"] = Relationship(back_populates="notion")
    notion_progresses: List["NotionProgress"] = Relationship(back_populates="notion")
    session_histories: List["SessionHistory"] = Relationship(back_populates="notion")


class Competence(SQLModel, table=True):
    __tablename__ = "competence"

    competence_id: UUID = Field(primary_key=True)
    referentiel_code: str = Field(max_length=50, unique=True)
    title: str
    level: str = Field(max_length=50)
    notion_id: UUID = Field(foreign_key="notion.notion_id")

    notion: Optional[Notion] = Relationship(back_populates="competences")
    progressions: List["Progression"] = Relationship(back_populates="competence")
    exercices: List["Exercice"] = Relationship(
        back_populates="competences",
        link_model=Generer,
    )


class Conversation(SQLModel, table=True):
    __tablename__ = "conversation"

    conversation_id: UUID = Field(primary_key=True)
    context_type: str = Field(default="chat_libre", max_length=50)
    status: str = Field(max_length=50)
    started_at: datetime
    updated_at: datetime
    title: str = Field(max_length=100)
    notion_id: Optional[UUID] = Field(default=None, foreign_key="notion.notion_id")
    sso_id: UUID = Field(foreign_key="user_.sso_id")

    user: Optional[User] = Relationship(back_populates="conversations")
    notion: Optional[Notion] = Relationship(back_populates="conversations")
    messages: List["Message"] = Relationship(back_populates="conversation")


class Message(SQLModel, table=True):
    __tablename__ = "message"

    message_id: UUID = Field(primary_key=True)
    role: str = Field(max_length=50)
    content: str
    sent_at: datetime
    conversation_id: UUID = Field(foreign_key="conversation.conversation_id")

    conversation: Optional[Conversation] = Relationship(back_populates="messages")


class Progression(SQLModel, table=True):
    __tablename__ = "progression"

    __table_args__ = (
        UniqueConstraint(
            "sso_id",
            "competence_id",
            name="progression_user_competence_unique",
        ),
    )

    progression_id: UUID = Field(primary_key=True)
    score: Decimal = Field(max_digits=3, decimal_places=2)
    updated_at: Optional[datetime] = None
    level: str = Field(max_length=50)
    attempts_count: int
    competence_id: UUID = Field(foreign_key="competence.competence_id")
    sso_id: UUID = Field(foreign_key="user_.sso_id")

    competence: Optional[Competence] = Relationship(back_populates="progressions")
    user: Optional[User] = Relationship(back_populates="progressions")


class Exercice(SQLModel, table=True):
    __tablename__ = "exercice"

    exercice_id: UUID = Field(primary_key=True)
    statement: str
    solution: str
    generated_at: datetime
    hints: Optional[str] = None

    competences: List[Competence] = Relationship(
        back_populates="exercices",
        link_model=Generer,
    )


class ModuleRecommendation(SQLModel, table=True):
    __tablename__ = "module_recommendation"

    __table_args__ = (
        UniqueConstraint(
            "sso_id",
            "notion_source_id",
            "notion_lacunaire_id",
            name="module_recommendation_user_source_lacunaire_unique",
        ),
    )

    recommendation_id: UUID = Field(primary_key=True)
    sso_id: UUID = Field(foreign_key="user_.sso_id")
    notion_source_id: UUID = Field(foreign_key="notion.notion_id")
    notion_lacunaire_id: UUID = Field(foreign_key="notion.notion_id")
    notion_lacunaire_nom: str = Field(max_length=200)
    count: int = Field(default=1)
    updated_at: datetime

    user: Optional[User] = Relationship(back_populates="recommendations")


class NotionProgress(SQLModel, table=True):
    __tablename__ = "notion_progress"

    __table_args__ = (
        UniqueConstraint(
            "sso_id",
            "notion_id",
            name="notion_progress_user_notion_unique",
        ),
    )

    progress_id: UUID = Field(primary_key=True)
    sso_id: UUID = Field(foreign_key="user_.sso_id")
    notion_id: UUID = Field(foreign_key="notion.notion_id")
    training_started: bool = Field(default=False)
    updated_at: datetime

    user: Optional[User] = Relationship(back_populates="notion_progresses")
    notion: Optional[Notion] = Relationship(back_populates="notion_progresses")


class SessionHistory(SQLModel, table=True):
    __tablename__ = "session_history"

    __table_args__ = (
        Index(
            "idx_session_history_user_notion_ended",
            "sso_id",
            "notion_id",
            "ended_at",
        ),
    )

    session_history_id: UUID = Field(primary_key=True)

    sso_id: UUID = Field(foreign_key="user_.sso_id")
    notion_id: UUID = Field(foreign_key="notion.notion_id")

    session_type: str = Field(max_length=50)

    score: Decimal = Field(max_digits=5, decimal_places=2)
    correct_count: int
    total_count: int

    qcm_correct: int = Field(default=0)
    qcm_total: int = Field(default=0)

    qro_correct: int = Field(default=0)
    qro_total: int = Field(default=0)

    sbs_correct: int = Field(default=0)
    sbs_total: int = Field(default=0)

    started_at: Optional[datetime] = None
    ended_at: datetime

    user: Optional[User] = Relationship(back_populates="session_histories")
    notion: Optional[Notion] = Relationship(back_populates="session_histories")