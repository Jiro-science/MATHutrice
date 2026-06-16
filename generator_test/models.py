from sqlmodel import SQLModel, Field, Relationship
from typing import Optional, List
from datetime import datetime
from decimal import Decimal


class Generer(SQLModel, table=True):
    __tablename__ = "generer"
    competence_id: str = Field(foreign_key="competence.competence_id", primary_key=True)
    exercice_id: str = Field(foreign_key="exercice.exercice_id", primary_key=True)


class User(SQLModel, table=True):
    __tablename__ = "user_"
    sso_id: str = Field(primary_key=True, max_length=36)
    created_at: datetime
    last_active: Optional[datetime] = None
    role: str = Field(max_length=50)
    email: str = Field(unique=True, max_length=50)
    name: str = Field(max_length=100)
    conversations: List["Conversation"] = Relationship(back_populates="user")
    progressions: List["Progression"] = Relationship(back_populates="user")
    recommendations: List["ModuleRecommendation"] = Relationship(back_populates="user")
    notion_progresses: List["NotionProgress"] = Relationship(back_populates="user")


class Notion(SQLModel, table=True):
    __tablename__ = "notion"
    notion_id: str = Field(primary_key=True, max_length=36)
    title: str = Field(max_length=50)
    description: str
    competences: List["Competence"] = Relationship(back_populates="notion")
    conversations: List["Conversation"] = Relationship(back_populates="notion")


class Competence(SQLModel, table=True):
    __tablename__ = "competence"
    competence_id: str = Field(primary_key=True, max_length=50)
    title: str = Field(max_length=50)
    description: str
    level: str = Field(max_length=50)
    notion_id: str = Field(foreign_key="notion.notion_id", max_length=36)
    notion: Optional[Notion] = Relationship(back_populates="competences")
    progressions: List["Progression"] = Relationship(back_populates="competence")
    exercices: List["Exercice"] = Relationship(
        back_populates="competences", link_model=Generer
    )


class Conversation(SQLModel, table=True):
    __tablename__ = "conversation"
    conversation_id: str = Field(primary_key=True, max_length=50)
    context_type: str = Field(default="chat_libre", max_length=50)
    status: str = Field(max_length=50)
    started_at: datetime
    updated_at: datetime
    title: str = Field(max_length=50)
    notion_id: Optional[str] = Field(
        default=None, foreign_key="notion.notion_id", max_length=36
    )
    sso_id: str = Field(foreign_key="user_.sso_id", max_length=36)
    user: Optional[User] = Relationship(back_populates="conversations")
    notion: Optional[Notion] = Relationship(back_populates="conversations")
    messages: List["Message"] = Relationship(back_populates="conversation")


class Message(SQLModel, table=True):
    __tablename__ = "message"
    message_id: str = Field(primary_key=True, max_length=50)
    role: str = Field(max_length=50)
    content: str
    sent_at: datetime
    conversation_id: str = Field(
        foreign_key="conversation.conversation_id", max_length=50
    )
    conversation: Optional[Conversation] = Relationship(back_populates="messages")


class Progression(SQLModel, table=True):
    __tablename__ = "progression"
    progression_id: str = Field(primary_key=True, max_length=50)
    score: Decimal = Field(max_digits=3, decimal_places=2)
    updated_at: datetime
    level: str = Field(max_length=50)
    attempts_count: int
    competence_id: str = Field(foreign_key="competence.competence_id", max_length=50)
    sso_id: str = Field(foreign_key="user_.sso_id", max_length=36)
    competence: Optional[Competence] = Relationship(back_populates="progressions")
    user: Optional[User] = Relationship(back_populates="progressions")


class Exercice(SQLModel, table=True):
    __tablename__ = "exercice"
    exercice_id: str = Field(primary_key=True, max_length=50)
    statement: str
    solution: str
    generated_at: datetime
    hints: Optional[str] = None
    competences: List[Competence] = Relationship(
        back_populates="exercices", link_model=Generer
    )


class ModuleRecommendation(SQLModel, table=True):
    """Lacunes inter-modules détectées pendant les sessions."""

    __tablename__ = "module_recommendation"
    recommendation_id: str = Field(primary_key=True, max_length=100)
    sso_id: str = Field(foreign_key="user_.sso_id", max_length=36)
    notion_source: str = Field(max_length=100)
    notion_lacunaire: str = Field(max_length=100)
    notion_lacunaire_nom: str = Field(max_length=200)
    count: int = Field(default=1)
    updated_at: datetime
    user: Optional[User] = Relationship(back_populates="recommendations")


class NotionProgress(SQLModel, table=True):
    """
    Suivi par (élève, notion) — indique si l'entraînement a été lancé.
    training_started = True dès que l'élève clique S'entraîner après le positionnement.
    Une fois True, on ne peut plus relancer le positionnement sur cette notion.
    """

    __tablename__ = "notion_progress"
    progress_id: str = Field(primary_key=True, max_length=100)
    sso_id: str = Field(foreign_key="user_.sso_id", max_length=36)
    notion_key: str = Field(max_length=100)
    training_started: bool = Field(default=False)
    updated_at: datetime
    user: Optional[User] = Relationship(back_populates="notion_progresses")
