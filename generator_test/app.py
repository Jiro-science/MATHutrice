from fastapi import FastAPI, Request, Depends, UploadFile, File
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    StreamingResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel
from typing import List as TList, Optional
from urllib.parse import unquote
from contextlib import asynccontextmanager
from sqlmodel import SQLModel, select, delete
from dotenv import load_dotenv
from database import engine, get_session, Session
from database import Session as DBSession
from apscheduler.schedulers.background import BackgroundScheduler
from decimal import Decimal
import models
import msal
import uvicorn
import shutil
import os
import uuid
import json
from datetime import datetime, timedelta


load_dotenv()


# ------------------------------------------------------------------
# Database
# ------------------------------------------------------------------


def create_db_and_tables():
    SQLModel.metadata.create_all(engine)


# ------------------------------------------------------------------
# Cleanup — supprime conversations + messages de plus de 24h
# ------------------------------------------------------------------


def cleanup_old_conversations():
    with DBSession(engine) as session:
        cutoff = datetime.utcnow() - timedelta(hours=24)

        old_conv_ids = session.exec(
            select(models.Conversation.conversation_id).where(
                models.Conversation.started_at < cutoff
            )
        ).all()

        for conv_id in old_conv_ids:
            session.exec(
                delete(models.Message).where(models.Message.conversation_id == conv_id)
            )
            session.exec(
                delete(models.Conversation).where(
                    models.Conversation.conversation_id == conv_id
                )
            )

        session.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_db_and_tables()

    scheduler = BackgroundScheduler()
    scheduler.add_job(cleanup_old_conversations, "interval", hours=1)
    scheduler.start()

    yield

    scheduler.shutdown()


# ------------------------------------------------------------------
# FastAPI app
# ------------------------------------------------------------------


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

SESSION_SECRET = os.getenv("SESSION_SECRET")

if not SESSION_SECRET:
    raise ValueError("SESSION_SECRET missing")

app.add_middleware(
        SessionMiddleware,
        secret_key=SESSION_SECRET,
        https_only=True,
        same_site="lax",
        max_age=3600,
)


# ------------------------------------------------------------------
# Paths
# ------------------------------------------------------------------


BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app.mount(
    "/static",
    StaticFiles(directory=os.path.join(BASE_DIR, "static")),
    name="static",
)

templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

UPLOAD_DIR = os.path.join(BASE_DIR, "rag_documents")
os.makedirs(UPLOAD_DIR, exist_ok=True)


# ------------------------------------------------------------------
# Azure AD configuration
# ------------------------------------------------------------------


CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
TENANT_ID = os.getenv("TENANT_ID")

if not CLIENT_ID:
    raise ValueError("CLIENT_ID missing")

if not CLIENT_SECRET:
    raise ValueError("CLIENT_SECRET missing")

if not TENANT_ID:
    raise ValueError("TENANT_ID missing")

REDIRECT_URL = "https://mathutrice-preprod.mde.epf.fr/auth"
SCOPE = ["User.Read"]
AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"


# ------------------------------------------------------------------
# MSAL helper
# ------------------------------------------------------------------


def get_msal_app():
    return msal.ConfidentialClientApplication(
        CLIENT_ID,
        authority=AUTHORITY,
        client_credential=CLIENT_SECRET,
    )


# ------------------------------------------------------------------
# Session helper
# ------------------------------------------------------------------


def get_current_user(request: Request):
    return request.session.get("user")


# ------------------------------------------------------------------
# LOGIN
# ------------------------------------------------------------------


@app.get("/test_login")
async def login(request: Request):
    msal_app = get_msal_app()

    #state en prod à activer dans /auth en prod
    state = str(uuid.uuid4())
    request.session["oauth_state"] = state

    print("STATE CREATED =", state)
    print("SESSION AFTER LOGIN =", dict(request.session))

    auth_url = msal_app.get_authorization_request_url(
        scopes=SCOPE,
        redirect_uri=REDIRECT_URL,
        state=state,
    )

    return RedirectResponse(auth_url)


# ------------------------------------------------------------------
# AUTH CALLBACK
# ------------------------------------------------------------------


@app.get("/auth")
async def auth_callback(
    request: Request,
    code: str = None,
    state: str = None,
    error: str = None,
    session: Session = Depends(get_session),
):
    if error:
        return HTMLResponse(
            f"<h2>❌ Erreur Azure</h2><pre>{error}</pre>",
            status_code=400,
        )

    # Vérification OAuth state désactivée temporairement en préprod
# À réactiver plus tard avec un state signé ou une session stable.
    # saved_state = request.session.get("oauth_state")

    # print("STATE URL =", state)
    # print("STATE SESSION =", saved_state)
    # print("SESSION CONTENT =", dict(request.session))

    # if not state or state != saved_state:
    #     return HTMLResponse("<h2>❌ Invalid OAuth state</h2>", status_code=400)

    # request.session.pop("oauth_state", None)

    msal_app = get_msal_app()

    result = msal_app.acquire_token_by_authorization_code(
        code,
        scopes=SCOPE,
        redirect_uri=REDIRECT_URL,
    )

    if "error" in result:
        return HTMLResponse(
            f"""
            <h2>❌ Erreur token</h2>
            <pre>{json.dumps(result, indent=2)}</pre>
            """,
            status_code=400,
        )

    claims = result.get("id_token_claims", {})
    email = claims.get("email") or claims.get("preferred_username")

    if not email or not email.endswith(("@epfedu.fr", "@epf.fr")):
        return HTMLResponse(
            """
            <h2>⛔ Accès refusé</h2>
            <p>Adresse EPF obligatoire.</p>
            """,
            status_code=403,
        )

    name = claims.get("name", "Unknown User")
    now = datetime.utcnow()

    existing_user = session.exec(
        select(models.User).where(models.User.email == email)
    ).first()

    from fonctions_python.session_generator import init_progressions_for_user

    if existing_user:
        existing_user.last_active = now
        session.add(existing_user)
        session.commit()

        try:
            init_progressions_for_user(existing_user.sso_id, session)
        except ValueError as e:
            return HTMLResponse(
                f"<h2>⚠ Erreur d'initialisation</h2><pre>{e}</pre>",
                status_code=500,
            )

        role = existing_user.role

    else:
        new_user = models.User(
            sso_id=uuid.uuid4(),
            name=name,
            email=email,
            role="Student",
            created_at=now,
            last_active=now,
        )

        session.add(new_user)
        session.commit()

        try:
            init_progressions_for_user(new_user.sso_id, session)
        except ValueError as e:
            return HTMLResponse(
                f"<h2>⚠ Erreur d'initialisation</h2><pre>{e}</pre>",
                status_code=500,
            )

        role = "Student"

    request.session["user"] = {
        "email": email,
        "name": name,
        "role": role,
        "impersonate": False,
    }

    if role in ("Teacher", "Admin"):
        return RedirectResponse("/teacher", status_code=302)

    return RedirectResponse("/", status_code=302)


# ------------------------------------------------------------------
# LOGOUT
# ------------------------------------------------------------------


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()

    logout_url = (
        f"https://login.microsoftonline.com/"
        f"{TENANT_ID}/oauth2/v2.0/logout"
        f"?post_logout_redirect_uri="
        f"https://mathutrice-preprod.mde.epf.fr/test_login"
    )

    return RedirectResponse(logout_url)


# ------------------------------------------------------------------
# STOP IMPERSONATE
# ------------------------------------------------------------------


@app.get("/impersonate/stop")
async def stop_impersonate(request: Request):
    current_user = get_current_user(request)

    if not current_user:
        return RedirectResponse("/test_login")

    if not current_user.get("impersonate"):
        return RedirectResponse("/")

    admin_email = current_user.get("real_admin")

    request.session["user"] = {
        "email": admin_email,
        "name": admin_email,
        "role": "Admin",
        "impersonate": False,
    }

    return RedirectResponse("/teacher")


# ------------------------------------------------------------------
# IMPERSONATE
# ------------------------------------------------------------------


@app.get("/impersonate/{email}")
async def impersonate(
    request: Request,
    email: str,
    session: Session = Depends(get_session),
):
    current_user = get_current_user(request)

    if not current_user or current_user.get("role") != "Admin":
        return HTMLResponse(
            "<h2>⛔ Accès refusé</h2>",
            status_code=403,
        )

    email = unquote(email)

    target = session.exec(select(models.User).where(models.User.email == email)).first()

    target_role = target.role if target else "Student"

    request.session["user"] = {
        "email": email,
        "name": target.name if target else email,
        "role": target_role,
        "impersonate": True,
        "real_admin": current_user["email"],
    }

    if target_role in ("Teacher", "Admin"):
        return RedirectResponse("/teacher", status_code=302)

    return RedirectResponse("/", status_code=302)


# ------------------------------------------------------------------
# TEACHER PAGE
# ------------------------------------------------------------------


@app.get("/teacher", response_class=HTMLResponse)
async def teacher_page(request: Request):
    user = get_current_user(request)

    if not user:
        return RedirectResponse("/test_login")

    if user.get("role") not in ("Teacher", "Admin"):
        return RedirectResponse("/")

    return templates.TemplateResponse(
        "upload.html",
        {"request": request},
    )


# ------------------------------------------------------------------
# PDF Upload
# ------------------------------------------------------------------


@app.post("/upload/pdf")
async def upload_pdf(
    request: Request,
    files: TList[UploadFile] = File(...),
):
    user = get_current_user(request)

    if not user:
        return JSONResponse(
            status_code=401,
            content={"detail": "Non connecté"},
        )

    if user.get("role") not in ("Teacher", "Admin"):
        return JSONResponse(
            status_code=403,
            content={"detail": "Accès refusé"},
        )

    email_prefix = user["email"].replace("@", "_at_")

    saved = []

    for file in files:
        if file.content_type != "application/pdf":
            continue

        filename = f"{email_prefix}__{file.filename}"
        dest = os.path.join(UPLOAD_DIR, filename)

        with open(dest, "wb") as f:
            shutil.copyfileobj(file.file, f)

        saved.append(file.filename)

    return {
        "ok": True,
        "message": f"{len(saved)} fichier(s) uploadé(s).",
        "files": saved,
    }


# ------------------------------------------------------------------
# PDF List
# ------------------------------------------------------------------


@app.get("/upload/list")
async def list_pdfs(request: Request):
    user = get_current_user(request)

    if not user:
        return JSONResponse(
            status_code=401,
            content={"detail": "Non connecté"},
        )

    if user.get("role") not in ("Teacher", "Admin"):
        return JSONResponse(
            status_code=403,
            content={"detail": "Accès refusé"},
        )

    email_prefix = user["email"].replace("@", "_at_")

    files = []

    for filename in os.listdir(UPLOAD_DIR):
        if not filename.endswith(".pdf"):
            continue

        if user.get("role") == "Teacher" and not filename.startswith(email_prefix):
            continue

        path = os.path.join(UPLOAD_DIR, filename)
        stat = os.stat(path)

        parts = filename.split("__", 1)

        display_name = parts[1] if len(parts) == 2 else filename
        uploader = parts[0].replace("_at_", "@") if len(parts) == 2 else "unknown"

        files.append(
            {
                "name": display_name,
                "uploader": uploader,
                "size": f"{stat.st_size / (1024 * 1024):.1f} MB",
                "date": datetime.fromtimestamp(stat.st_mtime).strftime(
                    "%d/%m/%Y %H:%M"
                ),
            }
        )

    files.sort(
        key=lambda x: x["date"],
        reverse=True,
    )

    return {
        "ok": True,
        "files": files,
    }


# ------------------------------------------------------------------
# HOME
# ------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def home_page(
    request: Request,
    session: Session = Depends(get_session),
):
    user = get_current_user(request)

    if not user:
        return RedirectResponse("/test_login")

    if user.get("role") == "Teacher":
        return RedirectResponse("/teacher")

    notions = session.exec(select(models.Notion.notion_id, models.Notion.title)).all()

    return templates.TemplateResponse(
        "home.html",
        {
            "request": request,
            "notions": notions,
            "name": user.get("name").split(" ")[0],
        },
    )


# ------------------------------------------------------------------
# MODULE PAGE
# ------------------------------------------------------------------

@app.get("/module", response_class=HTMLResponse)
async def module_page(
    request: Request,
    id: str,
    session: Session = Depends(get_session),
):
    user = get_current_user(request)

    if not user:
        return RedirectResponse("/test_login")

    if user.get("role") == "Teacher":
        return RedirectResponse("/teacher")

    try:
        notion_uuid = uuid.UUID(id)
    except ValueError:
        return HTMLResponse(
            "<h2>Identifiant de module invalide</h2>",
            status_code=400,
        )

    notion = session.exec(
        select(models.Notion).where(models.Notion.notion_id == notion_uuid)
    ).first()

    if not notion:
        return HTMLResponse(
            "<h2>Module introuvable</h2>",
            status_code=404,
        )

    return templates.TemplateResponse(
        "module.html",
        {
            "request": request,
            "notion": notion,
            "module_id": str(notion.notion_id),
            "notion_key": notion.referentiel_key,
        },
    )

# ------------------------------------------------------------------
# CHAT PAGE
# ------------------------------------------------------------------


@app.get("/chat", response_class=HTMLResponse)
async def chat_page(
    request: Request,
    session: Session = Depends(get_session),
):
    user = get_current_user(request)

    if not user:
        return RedirectResponse("/test_login")

    if user.get("role") == "Teacher":
        return RedirectResponse("/teacher")

    notions = session.exec(select(models.Notion.notion_id, models.Notion.title)).all()

    return templates.TemplateResponse(
        "chat.html",
        {
            "request": request,
            "notions": notions,
        },
    )


# ------------------------------------------------------------------
# QCM PAGE
# ------------------------------------------------------------------


@app.get("/qcm", response_class=HTMLResponse)
async def qcm_page(request: Request):
    user = get_current_user(request)

    if not user:
        return RedirectResponse("/test_login")

    if user.get("role") == "Teacher":
        return RedirectResponse("/teacher")

    return templates.TemplateResponse(
        "qcm.html",
        {"request": request},
    )


# ------------------------------------------------------------------
# Pydantic models
# ------------------------------------------------------------------


class QCMRequest(BaseModel):
    notion: str = "trigonométrie"
    niveau: str = "intermédiaire"
    n: int = 9


class ChatRequest(BaseModel):
    message: str
    conversation_id: Optional[str] = None


class SessionHistoryRequest(BaseModel):
    notion_key: str
    session_type: str
    score_total: int
    score_max: int
    type_stats: dict
    started_at: Optional[str] = None


class ResetRequest(BaseModel):
    notion_key: str


class SessionRequest(BaseModel):
    notion_key: str


class SubmitAnswerRequest(BaseModel):
    notion_key: str
    competences_dict: dict
    question_type: str
    question_niveau: str


class EvaluateQRORequest(BaseModel):
    question: str
    correct_answer: str
    user_answer: str


class FeedbackRequest(BaseModel):
    question: str
    correct_answer: str
    user_answer: str
    attempt: int
    competence: dict
    notion_nom: str
    notion_key: Optional[str] = None
    question_type: str


class NextTargetedRequest(BaseModel):
    notion_key: str
    competence_code: str


class TrainingStartedRequest(BaseModel):
    notion_key: str


class EvaluationRequest(BaseModel):
    notion_key: str
    n_questions: int = 10


# ------------------------------------------------------------------
# CONVERSATIONS — liste les conversations de l'utilisateur
# ------------------------------------------------------------------


@app.get("/conversations")
async def list_conversations(
    request: Request,
    session: Session = Depends(get_session),
):
    user = get_current_user(request)

    if not user:
        return JSONResponse(status_code=401, content={"detail": "Non connecté"})

    sso_id = session.exec(
        select(models.User.sso_id).where(models.User.email == user["email"])
    ).first()

    if not sso_id:
        return JSONResponse(
            status_code=404,
            content={"detail": "Utilisateur introuvable"},
        )

    conversations = session.exec(
        select(models.Conversation)
        .where(models.Conversation.sso_id == sso_id)
        .order_by(models.Conversation.updated_at.desc())
    ).all()

    return {
        "ok": True,
        "conversations": [
            {
                "id": str(c.conversation_id),
                "title": c.title,
                "updated_at": c.updated_at.isoformat(),
                "started_at": c.started_at.isoformat(),
            }
            for c in conversations
        ],
    }


# ------------------------------------------------------------------
# CONVERSATIONS — charge une conversation complète
# ------------------------------------------------------------------


@app.get("/conversations/{conversation_id}")
async def get_conversation(
    request: Request,
    conversation_id: str,
    session: Session = Depends(get_session),
):
    user = get_current_user(request)

    if not user:
        return JSONResponse(status_code=401, content={"detail": "Non connecté"})

    conv = session.exec(
        select(models.Conversation).where(
            models.Conversation.conversation_id == conversation_id
        )
    ).first()

    if not conv:
        return JSONResponse(
            status_code=404,
            content={"detail": "Conversation introuvable"},
        )

    messages = session.exec(
        select(models.Message)
        .where(models.Message.conversation_id == conversation_id)
        .order_by(models.Message.sent_at.asc())
    ).all()

    return {
        "ok": True,
        "conversation": {
            "id": str(conv.conversation_id),
            "title": conv.title,
        },
        "messages": [{"role": m.role, "content": m.content} for m in messages],
    }


# ------------------------------------------------------------------
# Chat streaming endpoint
# ------------------------------------------------------------------


@app.post("/chat/stream")
async def chat_stream_endpoint(
    request: Request,
    data: ChatRequest,
    session: Session = Depends(get_session),
):
    from fonctions_python.chatbot import chat_stream_with_history

    user = get_current_user(request)

    if not user:
        return JSONResponse(status_code=401, content={"detail": "Non connecté"})

    sso_id = session.exec(
    select(models.User.sso_id).where(models.User.email == user["email"])
    ).first()

    if not sso_id:
        return JSONResponse(
            status_code=404,
            content={"detail": "Utilisateur introuvable"},
        )

    now = datetime.utcnow()
    conversation_id = data.conversation_id

    if not conversation_id:
        conversation_id = uuid.uuid4()
        title = data.message[:47] + "..." if len(data.message) > 47 else data.message

        new_conv = models.Conversation(
            conversation_id=conversation_id,
            title=title,
            status="active",
            context_type="chat_libre",
            started_at=now,
            updated_at=now,
            sso_id=sso_id,
        )

        session.add(new_conv)
        session.commit()

    user_msg = models.Message(
        message_id=uuid.uuid4(),
        role="user",
        content=data.message,
        sent_at=now,
        conversation_id=conversation_id,
    )

    session.add(user_msg)
    session.commit()

    all_messages = session.exec(
        select(models.Message)
        .where(models.Message.conversation_id == conversation_id)
        .order_by(models.Message.sent_at.asc())
    ).all()

    history = [{"role": m.role, "content": m.content} for m in all_messages[-10:]]

    full_response = []

    def generate():
        for chunk in chat_stream_with_history(history):
            full_response.append(chunk)
            yield f"data: {chunk}\n\n"

        yield f"data: [CONV_ID:{conversation_id}]\n\n"
        yield "data: [DONE]\n\n"

        assistant_msg = models.Message(
            message_id=uuid.uuid4(),
            role="assistant",
            content="".join(full_response),
            sent_at=datetime.utcnow(),
            conversation_id=conversation_id,
        )

        session.add(assistant_msg)

        conv = session.exec(
            select(models.Conversation).where(
                models.Conversation.conversation_id == conversation_id
            )
        ).first()

        if conv:
            conv.updated_at = datetime.utcnow()
            session.add(conv)

        session.commit()

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ------------------------------------------------------------------
# Chat reset
# ------------------------------------------------------------------


@app.post("/chat/reset")
async def chat_reset_endpoint():
    from fonctions_python.chatbot import reset_conversation

    reset_conversation()

    return {
        "ok": True,
        "message": "Conversation reinitialisee",
    }


# ------------------------------------------------------------------
# Chat complete response
# ------------------------------------------------------------------


@app.post("/chat/complete")
async def chat_complete_endpoint(data: ChatRequest):
    from fonctions_python.chatbot import chat

    try:
        response = chat(data.message)

        return {
            "ok": True,
            "response": response,
        }

    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={
                "ok": False,
                "error": str(e),
            },
        )


# ------------------------------------------------------------------
# SESSION — reset scores d'une notion pour un élève
# ------------------------------------------------------------------


@app.post("/session/reset")
async def reset_session_endpoint(
    request: Request,
    data: ResetRequest,
    session: Session = Depends(get_session),
):
    from fonctions_python.session_generator import (
        get_competence_map_by_codes,
        get_notion_by_referentiel_key,
        init_progressions_for_user,
    )
    from fonctions_python.notion_catalogue import get_notion, UnknownNotionKeyError

    user = get_current_user(request)

    if not user:
        return JSONResponse(status_code=401, content={"detail": "Non connecté"})

    sso_id = session.exec(
        select(models.User.sso_id).where(models.User.email == user["email"])
    ).first()

    if not sso_id:
        return JSONResponse(
            status_code=404,
            content={"detail": "Utilisateur introuvable"},
        )

    try:
        catalogue = get_notion(data.notion_key, session)
    except UnknownNotionKeyError:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "Notion inconnue"},
        )

    notion = get_notion_by_referentiel_key(data.notion_key, session)

    if not notion:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "Notion inconnue en BDD"},
        )

    try:
        codes = [comp.code for comp in catalogue.competences]

        code_to_competence = get_competence_map_by_codes(codes, session)

        missing_codes = [
            code
            for code in codes
            if code not in code_to_competence
        ]

        if missing_codes:
            return JSONResponse(
                status_code=500,
                content={
                    "ok": False,
                    "error": "Compétences absentes en BDD",
                    "missing_codes": missing_codes,
                },
            )

        competence_ids = [
            competence.competence_id
            for competence in code_to_competence.values()
        ]

        rows = session.exec(
            select(models.Progression).where(
                models.Progression.sso_id == sso_id,
                models.Progression.competence_id.in_(competence_ids),
            )
        ).all()

        if len(rows) < len(competence_ids):
            init_progressions_for_user(sso_id, session)

            rows = session.exec(
                select(models.Progression).where(
                    models.Progression.sso_id == sso_id,
                    models.Progression.competence_id.in_(competence_ids),
                )
            ).all()

        now = datetime.utcnow()

        for prog in rows:
            prog.score = Decimal("0.50")
            prog.level = "moyen"
            prog.attempts_count = 0
            prog.updated_at = None
            session.add(prog)

        notion_prog = session.exec(
            select(models.NotionProgress).where(
                models.NotionProgress.sso_id == sso_id,
                models.NotionProgress.notion_id == notion.notion_id,
            )
        ).first()

        if notion_prog:
            notion_prog.training_started = False
            notion_prog.updated_at = now
            session.add(notion_prog)

        session.commit()

        return {
            "ok": True,
            "reset_count": len(rows),
            "notion_key": data.notion_key,
        }

    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": str(e)},
        )


# ------------------------------------------------------------------
# SESSION PAGE
# ------------------------------------------------------------------


@app.get("/session", response_class=HTMLResponse)
async def session_page(request: Request):
    user = get_current_user(request)

    if not user:
        return RedirectResponse("/test_login")

    if user.get("role") == "Teacher":
        return RedirectResponse("/teacher")

    return templates.TemplateResponse("session.html", {"request": request})


# ------------------------------------------------------------------
# SESSION — check première session
# ------------------------------------------------------------------


@app.get("/session/check")
async def check_session(
    request: Request,
    notion_key: str,
    session: Session = Depends(get_session),
):
    from fonctions_python.session_generator import (
        get_notion_by_referentiel_key,
        is_first_session,
    )

    user = get_current_user(request)

    if not user:
        return JSONResponse(status_code=401, content={"detail": "Non connecté"})

    sso_id = session.exec(
        select(models.User.sso_id).where(models.User.email == user["email"])
    ).first()

    if not sso_id:
        return JSONResponse(
            status_code=404,
            content={"detail": "Utilisateur introuvable"},
        )

    notion = get_notion_by_referentiel_key(notion_key, session)

    if not notion:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "Notion inconnue"},
        )

    try:
        first = is_first_session(notion_key, sso_id, session)
    except ValueError as e:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": str(e)},
        )

    notion_prog = session.exec(
        select(models.NotionProgress).where(
            models.NotionProgress.sso_id == sso_id,
            models.NotionProgress.notion_id == notion.notion_id,
        )
    ).first()

    training_started = notion_prog.training_started if notion_prog else False

    return {
        "ok": True,
        "first_session": first,
        "training_started": training_started,
    }


# ------------------------------------------------------------------
# SESSION — génération positionnement
# ------------------------------------------------------------------


@app.post("/session/positioning")
async def positioning_endpoint(
    request: Request,
    data: SessionRequest,
    session: Session = Depends(get_session),
):
    from fonctions_python.session_generator import generate_positioning_session

    user = get_current_user(request)

    if not user:
        return JSONResponse(status_code=401, content={"detail": "Non connecté"})

    sso_id = session.exec(
        select(models.User.sso_id).where(models.User.email == user["email"])
    ).first()

    if not sso_id:
        return JSONResponse(
            status_code=404,
            content={"detail": "Utilisateur introuvable"},
        )

    try:
        result = generate_positioning_session(data.notion_key, sso_id, session)
        return {"ok": True, **result}

    except ValueError as e:
        return JSONResponse(status_code=400, content={"ok": False, "error": str(e)})

    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


# ------------------------------------------------------------------
# SESSION — génération question entraînement
# ------------------------------------------------------------------


@app.post("/session/next")
async def next_question_endpoint(
    request: Request,
    data: SessionRequest,
    session: Session = Depends(get_session),
):
    from fonctions_python.session_generator import generate_next_question

    user = get_current_user(request)

    if not user:
        return JSONResponse(status_code=401, content={"detail": "Non connecté"})

    sso_id = session.exec(
        select(models.User.sso_id).where(models.User.email == user["email"])
    ).first()

    if not sso_id:
        return JSONResponse(
            status_code=404,
            content={"detail": "Utilisateur introuvable"},
        )

    try:
        result = generate_next_question(data.notion_key, sso_id, session)
        return {"ok": True, **result}

    except ValueError as e:
        return JSONResponse(status_code=400, content={"ok": False, "error": str(e)})

    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


# ------------------------------------------------------------------
# SESSION — soumettre une réponse
# ------------------------------------------------------------------


@app.post("/session/submit")
async def submit_answer_endpoint(
    request: Request,
    data: SubmitAnswerRequest,
    session: Session = Depends(get_session),
):
    from fonctions_python.session_generator import persist_score_update

    user = get_current_user(request)

    if not user:
        return JSONResponse(status_code=401, content={"detail": "Non connecté"})

    sso_id = session.exec(
        select(models.User.sso_id).where(models.User.email == user["email"])
    ).first()

    if not sso_id:
        return JSONResponse(
            status_code=404,
            content={"detail": "Utilisateur introuvable"},
        )

    try:
        result = persist_score_update(
            sso_id=sso_id,
            competences_dict=data.competences_dict,
            question_type=data.question_type,
            question_niveau=data.question_niveau,
            db=session,
        )

        return {"ok": True, "new_scores": result}

    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


# ------------------------------------------------------------------
# SESSION — scores réels d'un élève pour une notion
# ------------------------------------------------------------------


@app.get("/session/scores")
async def get_scores_endpoint(
    request: Request,
    notion_key: str,
    session: Session = Depends(get_session),
):
    from fonctions_python.session_generator import build_notion_data_with_scores

    user = get_current_user(request)

    if not user:
        return JSONResponse(status_code=401, content={"detail": "Non connecté"})

    sso_id = session.exec(
        select(models.User.sso_id).where(models.User.email == user["email"])
    ).first()

    if not sso_id:
        return JSONResponse(
            status_code=404,
            content={"detail": "Utilisateur introuvable"},
        )

    try:
        notion_data = build_notion_data_with_scores(notion_key, sso_id, session)
    except ValueError:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "Notion inconnue"},
        )

    try:
        competences = []

        for comp in notion_data["competences"]:
            score = float(comp.get("score", 0.5))

            competences.append(
                {
                    "code": comp.get("code"),
                    "nom": comp.get("title") or comp.get("code"),
                    "niveau": comp.get("niveau", "basique"),
                    "score": round(score, 2),
                    "score_pct": round(score * 100),
                }
            )

        global_score = (
            sum(c["score"] for c in competences) / len(competences)
            if competences
            else 0.0
        )

        return {
            "ok": True,
            "notion_key": notion_key,
            "notion_nom": notion_data.get("notion_title", notion_key),
            "global_score": round(global_score, 2),
            "global_score_pct": round(global_score * 100),
            "competences": competences,
        }

    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": str(e)},
        )


# ------------------------------------------------------------------
# SESSION — enregistrer historique
# ------------------------------------------------------------------


@app.post("/session/history")
async def save_session_history_endpoint(
    request: Request,
    data: SessionHistoryRequest,
    session: Session = Depends(get_session),
):
    from fonctions_python.session_generator import get_notion_by_referentiel_key

    user = get_current_user(request)

    if not user:
        return JSONResponse(status_code=401, content={"detail": "Non connecté"})

    sso_id = session.exec(
        select(models.User.sso_id).where(models.User.email == user["email"])
    ).first()

    if not sso_id:
        return JSONResponse(
            status_code=404,
            content={"detail": "Utilisateur introuvable"},
        )

    notion = get_notion_by_referentiel_key(data.notion_key, session)

    if not notion:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "Notion inconnue"},
        )

    try:
        total = max(0, int(data.score_max or 0))
        correct = max(0, int(data.score_total or 0))

        score_pct = Decimal("0.00")

        if total > 0:
            score_pct = Decimal(str(round((correct / total) * 100, 2)))

        qcm = data.type_stats.get("qcm", [0, 0])
        qro = data.type_stats.get("qro", [0, 0])
        sbs = data.type_stats.get("sbs", [0, 0])

        started_at = None

        if data.started_at:
            try:
                started_at = datetime.fromisoformat(
                    data.started_at.replace("Z", "+00:00")
                ).replace(tzinfo=None)
            except Exception:
                started_at = None

        row = models.SessionHistory(
            session_history_id=uuid.uuid4(),
            sso_id=sso_id,
            notion_id=notion.notion_id,
            session_type=data.session_type,
            score=score_pct,
            correct_count=correct,
            total_count=total,
            qcm_correct=int(qcm[0] or 0),
            qcm_total=int(qcm[1] or 0),
            qro_correct=int(qro[0] or 0),
            qro_total=int(qro[1] or 0),
            sbs_correct=int(sbs[0] or 0),
            sbs_total=int(sbs[1] or 0),
            started_at=started_at,
            ended_at=datetime.utcnow(),
        )

        session.add(row)
        session.commit()

        return {
            "ok": True,
            "session_history_id": str(row.session_history_id),
        }

    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": str(e)},
        )


# ------------------------------------------------------------------
# SESSION — lire historique
# ------------------------------------------------------------------


@app.get("/session/history")
async def get_session_history_endpoint(
    request: Request,
    notion_key: str,
    session: Session = Depends(get_session),
):
    from fonctions_python.session_generator import get_notion_by_referentiel_key

    user = get_current_user(request)

    if not user:
        return JSONResponse(status_code=401, content={"detail": "Non connecté"})

    sso_id = session.exec(
        select(models.User.sso_id).where(models.User.email == user["email"])
    ).first()

    if not sso_id:
        return JSONResponse(
            status_code=404,
            content={"detail": "Utilisateur introuvable"},
        )

    notion = get_notion_by_referentiel_key(notion_key, session)

    if not notion:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "Notion inconnue"},
        )

    try:
        rows = session.exec(
            select(models.SessionHistory)
            .where(
                models.SessionHistory.sso_id == sso_id,
                models.SessionHistory.notion_id == notion.notion_id,
            )
            .order_by(models.SessionHistory.ended_at.desc())
        ).all()

        history = []

        for row in rows[:8]:
            history.append(
                {
                    "type": row.session_type,
                    "date": row.ended_at.strftime("%d/%m/%Y %H:%M"),
                    "notion": notion.title,
                    "score_pct": round(float(row.score)),
                    "score": row.correct_count,
                    "total": row.total_count,
                    "qcm": [row.qcm_correct, row.qcm_total],
                    "qro": [row.qro_correct, row.qro_total],
                    "sbs": [row.sbs_correct, row.sbs_total],
                }
            )

        return {
            "ok": True,
            "history": history,
        }

    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": str(e)},
        )


# ------------------------------------------------------------------
# SESSION — évaluation QRO
# ------------------------------------------------------------------


@app.post("/session/evaluate_qro")
async def evaluate_qro_endpoint(
    request: Request,
    data: EvaluateQRORequest,
):
    from fonctions_python.type_questions.qro_generator import evaluate_answer

    user = get_current_user(request)

    if not user:
        return JSONResponse(status_code=401, content={"detail": "Non connecté"})

    try:
        q = {
            "question": data.question,
            "correct_answer": data.correct_answer,
        }

        correct, feedback = evaluate_answer(q, data.user_answer)

        return {
            "ok": True,
            "correct": correct,
            "feedback": feedback,
        }

    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


# ------------------------------------------------------------------
# SESSION — feedback progressif
# ------------------------------------------------------------------


@app.post("/session/feedback")
async def feedback_endpoint(
    request: Request,
    data: FeedbackRequest,
    session: Session = Depends(get_session),
):
    from fonctions_python.base_generator import client, MODEL
    from lacune_evaluation.LLM_as_Evaluator import diagnostiquer_depuis_competence
    from fonctions_python.session_generator import (
        get_competence_map_by_codes,
        get_notion_by_referentiel_key,
    )
    from fonctions_python.notion_catalogue import get_notion

    user = get_current_user(request)

    if not user:
        return JSONResponse(status_code=401, content={"detail": "Non connecté"})

    try:
        instructions = {
            1: "Donne un indice court et orientant (2 phrases max). Ne donne pas la methode, juste une piste de reflexion.",
            2: "Explique la methode a suivre sans donner le resultat. Rappelle la definition ou la regle cle. 3-4 phrases.",
            3: "Donne une explication complete avec la formule ou la regle exacte. C'est la derniere chance. 4-5 phrases.",
        }

        instruction = instructions[min(data.attempt, 3)]

        prompt = (
            "Tu es un tuteur de mathematiques pour etudiants de premiere annee.\n"
            f"Tentative {data.attempt}/3.\n\n"
            f"Question : {data.question}\n"
            f"Reponse eleve : {data.user_answer}\n"
            "Competence : "
            + data.competence.get("nom", "")
            + " niveau "
            + data.competence.get("niveau", "")
            + "\n\n"
            "Consigne : "
            + instruction
            + "\n"
            "Ne donne JAMAIS la bonne reponse. Utilise le tu. Sois concis. Pas de JSON ni balises.\n"
        )

        response = client.chat.complete(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
        )

        feedback = response.choices[0].message.content.strip()

        cross_module_reco = None

        if data.attempt >= 2 and data.notion_key:
            try:
                sso_id = session.exec(
                    select(models.User.sso_id).where(
                        models.User.email == user["email"]
                    )
                ).first()

                if not sso_id:
                    return JSONResponse(
                        status_code=404,
                        content={"detail": "Utilisateur introuvable"},
                    )

                source_notion = get_notion_by_referentiel_key(
                    data.notion_key, session
                )

                if not source_notion:
                    return JSONResponse(
                        status_code=400,
                        content={"ok": False, "error": "Notion source inconnue"},
                    )

                result_diag = diagnostiquer_depuis_competence(
                    notion=data.notion_nom,
                    niveau=data.competence.get("niveau", "basique"),
                    enonce=data.question,
                    reponse_correcte=data.correct_answer,
                    reponse_etudiant=data.user_answer,
                    competence_cible=data.competence,
                    nb_tentatives=data.attempt,
                )

                diag = result_diag.get("diagnostic", {}).get("diagnostic", {})
                lacunaires = diag.get("competences_lacunaires", [])

                notion_courante = get_notion(data.notion_key, session)
                notion_codes_courants = [
                    c.code for c in notion_courante.competences
                ]

                for lac in lacunaires:
                    lacune_code = lac.get("code")

                    if not lacune_code:
                        continue

                    if (
                        lac.get("source") == "detectee_passe2"
                        and lacune_code not in notion_codes_courants
                    ):
                        lacune_competence = get_competence_map_by_codes(
                            [lacune_code], session
                        ).get(lacune_code)

                        if not lacune_competence:
                            # Politique stricte en écriture (docs/adr/0002) :
                            # une recommandation ne doit jamais pointer vers
                            # une compétence lacunaire absente de la BDD.
                            raise ValueError(
                                f"Compétence lacunaire introuvable en BDD : "
                                f"{lacune_code}"
                            )

                        lacunaire_notion = session.exec(
                            select(models.Notion).where(
                                models.Notion.notion_id
                                == lacune_competence.notion_id
                            )
                        ).first()

                        if not lacunaire_notion:
                            # Idem : la notion propriétaire de cette
                            # compétence doit exister en BDD, sinon on ne
                            # peut pas écrire la recommandation en toute
                            # sécurité.
                            raise ValueError(
                                f"Notion introuvable en BDD pour la "
                                f"compétence lacunaire {lacune_code}"
                            )

                        if lacunaire_notion.notion_id == source_notion.notion_id:
                            continue

                        notion_trouvee_key = lacunaire_notion.referentiel_key
                        notion_trouvee_nom = lacunaire_notion.title

                        existing = session.exec(
                            select(models.ModuleRecommendation).where(
                                models.ModuleRecommendation.sso_id == sso_id,
                                models.ModuleRecommendation.notion_source_id
                                == source_notion.notion_id,
                                models.ModuleRecommendation.notion_lacunaire_id
                                == lacunaire_notion.notion_id,
                            )
                        ).first()

                        now = datetime.utcnow()

                        if existing:
                            existing.count += 1
                            existing.updated_at = now
                            session.add(existing)
                            reco_count = existing.count

                        else:
                            new_reco = models.ModuleRecommendation(
                                recommendation_id=uuid.uuid4(),
                                sso_id=sso_id,
                                notion_source_id=source_notion.notion_id,
                                notion_lacunaire_id=lacunaire_notion.notion_id,
                                notion_lacunaire_nom=notion_trouvee_nom,
                                count=1,
                                updated_at=now,
                            )

                            session.add(new_reco)
                            reco_count = 1

                        session.commit()

                        cross_module_reco = {
                            "notion_id": str(lacunaire_notion.notion_id),
                            "notion_key": notion_trouvee_key,
                            "notion_nom": notion_trouvee_nom,
                            "count": reco_count,
                        }

                        break

            except ValueError:
                # Politique stricte en écriture (docs/adr/0002) : une
                # recommandation cassée par une divergence référentiel/BDD
                # doit remonter, pas être avalée en silence.
                raise

            except Exception as reco_err:
                print("Reco error:", reco_err)

        return {
            "ok": True,
            "feedback": feedback,
            "can_retry": data.attempt < 3,
            "cross_module_reco": cross_module_reco,
        }

    except Exception as e:
        import traceback

        print("FEEDBACK ERROR:", traceback.format_exc())

        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": str(e)},
        )


# ------------------------------------------------------------------
# SESSION — question ciblée sur une compétence spécifique
# ------------------------------------------------------------------


@app.post("/session/next_targeted")
async def next_targeted_endpoint(
    request: Request,
    data: NextTargetedRequest,
    session: Session = Depends(get_session),
):
    import random
    from fonctions_python.type_questions.qcm_generator import generate_qcm_test
    from fonctions_python.type_questions.qro_generator import generate_qro_test
    from fonctions_python.session_generator import build_notion_data_with_scores

    user = get_current_user(request)

    if not user:
        return JSONResponse(status_code=401, content={"detail": "Non connecté"})

    sso_id = session.exec(
        select(models.User.sso_id).where(models.User.email == user["email"])
    ).first()

    if not sso_id:
        return JSONResponse(
            status_code=404,
            content={"detail": "Utilisateur introuvable"},
        )

    try:
        notion_data = build_notion_data_with_scores(data.notion_key, sso_id, session)
    except ValueError:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "Notion inconnue"},
        )

    try:
        notion_description = notion_data["notion_nom"]
        notion_title = notion_data["notion_title"]

        comp = next(
            (
                c
                for c in notion_data["competences"]
                if c["code"] == data.competence_code
            ),
            None,
        )

        if not comp:
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": "Compétence inconnue"},
            )

        qtype = random.choice(["qcm", "qro"])

        if qtype == "qcm":
            questions = generate_qcm_test(notion_description, [comp])

            if not questions:
                return JSONResponse(
                    status_code=500,
                    content={"ok": False, "error": "Aucune QCM générée"},
                )

            question = questions[0]
            question["type"] = "qcm"

        else:
            questions = generate_qro_test(notion_description, [comp])

            if not questions:
                return JSONResponse(
                    status_code=500,
                    content={"ok": False, "error": "Aucune QRO générée"},
                )

            question = questions[0]
            question["type"] = "qro"

        question["notion_nom"] = notion_title
        question["niveau"] = comp.get("niveau", "basique")

        return {
            "ok": True,
            "questions": [question],
            "notion_nom": notion_title,
        }

    except Exception as e:
        import traceback
        print("NEXT_TARGETED ERROR:", traceback.format_exc())

        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": str(e)},
        )


# ------------------------------------------------------------------
# SESSION — marquer l'entraînement comme commencé
# ------------------------------------------------------------------


@app.post("/session/training_started")
async def mark_training_started(
    request: Request,
    data: TrainingStartedRequest,
    session: Session = Depends(get_session),
):
    from fonctions_python.session_generator import get_notion_by_referentiel_key

    user = get_current_user(request)

    if not user:
        return JSONResponse(status_code=401, content={"detail": "Non connecté"})

    sso_id = session.exec(
        select(models.User.sso_id).where(models.User.email == user["email"])
    ).first()

    if not sso_id:
        return JSONResponse(
            status_code=404,
            content={"detail": "Utilisateur introuvable"},
        )

    notion = get_notion_by_referentiel_key(data.notion_key, session)

    if not notion:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "Notion inconnue"},
        )

    try:
        existing = session.exec(
            select(models.NotionProgress).where(
                models.NotionProgress.sso_id == sso_id,
                models.NotionProgress.notion_id == notion.notion_id,
            )
        ).first()

        now = datetime.utcnow()

        if existing:
            existing.training_started = True
            existing.updated_at = now
            session.add(existing)

        else:
            session.add(
                models.NotionProgress(
                    progress_id=uuid.uuid4(),
                    sso_id=sso_id,
                    notion_id=notion.notion_id,
                    training_started=True,
                    updated_at=now,
                )
            )

        session.commit()

        return {"ok": True}

    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": str(e)},
        )


# ------------------------------------------------------------------
# SESSION — génération test d'évaluation
# ------------------------------------------------------------------


@app.post("/session/evaluation")
async def evaluation_endpoint(
    request: Request,
    data: EvaluationRequest,
    session: Session = Depends(get_session),
):
    import random
    from fonctions_python.main import generate_mixed_test
    from fonctions_python.session_generator import build_notion_data_with_scores

    user = get_current_user(request)

    if not user:
        return JSONResponse(status_code=401, content={"detail": "Non connecté"})

    sso_id = session.exec(
        select(models.User.sso_id).where(models.User.email == user["email"])
    ).first()

    if not sso_id:
        return JSONResponse(
            status_code=404,
            content={"detail": "Utilisateur introuvable"},
        )

    try:
        n = max(5, min(20, data.n_questions))

        n_bas = max(1, round(n * 0.2))
        n_sol = max(1, round(n * 0.5))
        n_exp = max(1, n - n_bas - n_sol)

        def split(total):
            if total <= 0:
                return 0, 0, 0

            qcm = max(1, round(total * 0.5))
            qro = max(1, round(total * 0.3))
            sbs = max(0, total - qcm - qro)

            return qcm, qro, sbs

        notion_data = build_notion_data_with_scores(data.notion_key, sso_id, session)
        notion_nom = notion_data["notion_title"]

        q_bas = generate_mixed_test(
            notion=data.notion_key,
            niveau="basique",
            n_qcm=split(n_bas)[0],
            n_qro=split(n_bas)[1],
            n_steps=split(n_bas)[2],
            notion_data_override=notion_data,
        )

        q_sol = generate_mixed_test(
            notion=data.notion_key,
            niveau="solide",
            n_qcm=split(n_sol)[0],
            n_qro=split(n_sol)[1],
            n_steps=split(n_sol)[2],
            notion_data_override=notion_data,
        )

        q_exp = generate_mixed_test(
            notion=data.notion_key,
            niveau="expert",
            n_qcm=split(n_exp)[0],
            n_qro=split(n_exp)[1],
            n_steps=split(n_exp)[2],
            notion_data_override=notion_data,
        )

        questions = q_bas + q_sol + q_exp

        seen = set()
        unique_questions = []

        for q in questions:
            comp_code = (q.get("competence_cible") or {}).get("code", "")
            key = comp_code + q.get("type", "")

            if key not in seen:
                seen.add(key)
                unique_questions.append(q)

        questions = unique_questions
        random.shuffle(questions)

        return {
            "ok": True,
            "questions": questions,
            "notion_nom": notion_nom,
            "n_questions": len(questions),
        }

    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


# ------------------------------------------------------------------
# SESSION — recommandations inter-modules
# ------------------------------------------------------------------


@app.get("/session/recommendations")
async def get_recommendations_endpoint(
    request: Request,
    notion_key: str,
    session: Session = Depends(get_session),
):
    from fonctions_python.session_generator import get_notion_by_referentiel_key

    user = get_current_user(request)

    if not user:
        return JSONResponse(status_code=401, content={"detail": "Non connecté"})

    sso_id = session.exec(
        select(models.User.sso_id).where(models.User.email == user["email"])
    ).first()

    if not sso_id:
        return JSONResponse(
            status_code=404,
            content={"detail": "Utilisateur introuvable"},
        )

    source_notion = get_notion_by_referentiel_key(notion_key, session)

    if not source_notion:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "Notion inconnue"},
        )

    try:
        rows = session.exec(
            select(models.ModuleRecommendation)
            .where(
                models.ModuleRecommendation.sso_id == sso_id,
                models.ModuleRecommendation.notion_source_id == source_notion.notion_id,
                models.ModuleRecommendation.count >= 2,
            )
            .order_by(models.ModuleRecommendation.count.desc())
        ).all()

        recommendations = []

        for reco in rows:
            lacunaire_notion = session.exec(
                select(models.Notion).where(
                    models.Notion.notion_id == reco.notion_lacunaire_id
                )
            ).first()

            if not lacunaire_notion:
                continue

            recommendations.append(
                {
                    "notion_id": str(lacunaire_notion.notion_id),
                    "notion_key": lacunaire_notion.referentiel_key,
                    "notion_nom": reco.notion_lacunaire_nom,
                    "title": lacunaire_notion.title,
                    "description": lacunaire_notion.description,
                    "count": reco.count,
                }
            )

        return {
            "ok": True,
            "recommendations": recommendations,
        }

    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": str(e)},
        )


# ------------------------------------------------------------------
# Run app
# ------------------------------------------------------------------


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
    )