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
from fonctions_python.chatbot import (
    chat,
    chat_stream,
    chat_stream_with_history,
    reset_conversation,
)
from test_format_generator.QCM import generate_qcm_statement
from apscheduler.schedulers.background import BackgroundScheduler

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
    from database import Session as DBSession

    with DBSession(engine) as session:
        cutoff = datetime.utcnow() - timedelta(hours=24)

        old_conv_ids = session.exec(
            select(models.Conversation.conversation_id).where(
                models.Conversation.started_at < cutoff
            )
        ).all()

        for conv_id in old_conv_ids:
            session.exec(
                delete(models.Message).where(
                    models.Message.conversation_id == conv_id
                )
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

    # Lance le scheduler de nettoyage toutes les heures
    scheduler = BackgroundScheduler()
    scheduler.add_job(cleanup_old_conversations, "interval", hours=1)
    scheduler.start()

    yield

    scheduler.shutdown()


# ------------------------------------------------------------------
# FastAPI app
# ------------------------------------------------------------------

app = FastAPI(lifespan=lifespan)

# Allow frontend requests
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Session middleware using signed cookies
# Stores authenticated user information securely
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SESSION_SECRET"),
    https_only=True,
    same_site="lax",
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

templates = Jinja2Templates(
    directory=os.path.join(BASE_DIR, "templates")
)

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
    """
    Creates MSAL confidential client used for OAuth authentication.
    """

    return msal.ConfidentialClientApplication(
        CLIENT_ID,
        authority=AUTHORITY,
        client_credential=CLIENT_SECRET,
    )


# ------------------------------------------------------------------
# Session helper
# ------------------------------------------------------------------


def get_current_user(request: Request):
    """
    Returns current authenticated user from session.
    """

    return request.session.get("user")


# ------------------------------------------------------------------
# LOGIN
# Redirects user to Microsoft login page
# ------------------------------------------------------------------


@app.get("/test_login")
async def login(request: Request):

    msal_app = get_msal_app()

    # Generate unique OAuth state
    # Used to protect against CSRF attacks
    state = str(uuid.uuid4())

    request.session["oauth_state"] = state

    auth_url = msal_app.get_authorization_request_url(
        scopes=SCOPE,
        redirect_uri=REDIRECT_URL,
        state=state,
    )

    return RedirectResponse(auth_url)


# ------------------------------------------------------------------
# AUTH CALLBACK
# Handles Microsoft OAuth response
# ------------------------------------------------------------------


@app.get("/auth")
async def auth_callback(
    request: Request,
    code: str = None,
    state: str = None,
    error: str = None,
    session: Session = Depends(get_session),
):

    # Handle Azure authentication errors
    if error:
        return HTMLResponse(
            f"<h2>❌ Erreur Azure</h2><pre>{error}</pre>",
            status_code=400,
        )

    # Verify OAuth state
    # Prevents CSRF attacks
    # desactivé en préprod — marche sur un seul nom de domaine
    # saved_state = request.session.get("oauth_state")
    # if not state or state != saved_state:
    #     return HTMLResponse("<h2>❌ Invalid OAuth state</h2>", status_code=400)

    msal_app = get_msal_app()

    # Exchange authorization code for token
    result = msal_app.acquire_token_by_authorization_code(
        code,
        scopes=SCOPE,
        redirect_uri=REDIRECT_URL,
    )

    # Handle token errors
    if "error" in result:
        return HTMLResponse(
            f"""
            <h2>❌ Erreur token</h2>
            <pre>{json.dumps(result, indent=2)}</pre>
            """,
            status_code=400,
        )

    # Extract user claims
    claims = result.get("id_token_claims", {})

    email = claims.get("email") or claims.get("preferred_username")

    # Restrict access to EPF domains only
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

    # Search user in database
    existing_user = session.exec(
        select(models.User).where(models.User.email == email)
    ).first()

    # Existing user
    if existing_user:

        existing_user.last_active = now

        session.add(existing_user)
        session.commit()

        role = existing_user.role

    # First login → create default student
    else:

        new_user = models.User(
            sso_id=str(uuid.uuid4()),
            name=name,
            email=email,
            role="Student",
            created_at=now,
            last_active=now,
        )

        session.add(new_user)
        session.commit()

        role = "Student"

    # Store authenticated user in session
    request.session["user"] = {
        "email": email,
        "name": name,
        "role": role,
        "impersonate": False,
    }

    # Redirect depending on role
    if role in ("Teacher", "Admin"):
        return RedirectResponse("/teacher", status_code=302)

    return RedirectResponse("/", status_code=302)


# ------------------------------------------------------------------
# LOGOUT
# Clears local session and Microsoft session
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
# Restores original admin session
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
# Allows admin to simulate another user
# DEV / ADMIN ONLY
# ------------------------------------------------------------------


@app.get("/impersonate/{email}")
async def impersonate(
    request: Request,
    email: str,
    session: Session = Depends(get_session),
):

    current_user = get_current_user(request)

    # Only admins can impersonate
    if not current_user or current_user.get("role") != "Admin":
        return HTMLResponse(
            "<h2>⛔ Accès refusé</h2>",
            status_code=403,
        )

    email = unquote(email)

    target = session.exec(
        select(models.User).where(models.User.email == email)
    ).first()

    # Default role if user does not exist
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
# Teacher/Admin only
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
# Teacher/Admin only
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
# Admin sees all files
# Teacher sees only own files
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

        # Teachers only see their own files
        if (
            user.get("role") == "Teacher"
            and not filename.startswith(email_prefix)
        ):
            continue

        path = os.path.join(UPLOAD_DIR, filename)

        stat = os.stat(path)

        parts = filename.split("__", 1)

        display_name = (
            parts[1] if len(parts) == 2 else filename
        )

        uploader = (
            parts[0].replace("_at_", "@")
            if len(parts) == 2
            else "unknown"
        )

        files.append({
            "name": display_name,
            "uploader": uploader,
            "size": f"{stat.st_size / (1024 * 1024):.1f} MB",
            "date": datetime.fromtimestamp(
                stat.st_mtime
            ).strftime("%d/%m/%Y %H:%M"),
        })

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
# Student/Admin access
# ------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def home_page(
    request: Request,
    session: Session = Depends(get_session),
):

    user = get_current_user(request)

    if not user:
        return RedirectResponse("/test_login")

    # Teacher cannot access student pages
    if user.get("role") == "Teacher":
        return RedirectResponse("/teacher")

    notions = session.exec(
        select(models.Notion.notion_id, models.Notion.title)
    ).all()

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
# Student/Admin access
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

    notion = session.exec(
        select(
            models.Notion.title,
            models.Notion.description,
        ).where(models.Notion.notion_id == id)
    ).first()

    return templates.TemplateResponse(
        "module.html",
        {
            "request": request,
            "notion": notion,
        },
    )


# ------------------------------------------------------------------
# CHAT PAGE
# Student/Admin access
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

    notions = session.exec(
        select(models.Notion.notion_id, models.Notion.title)
    ).all()

    return templates.TemplateResponse(
        "chat.html",
        {
            "request": request,
            "notions": notions,
        },
    )


# ------------------------------------------------------------------
# QCM PAGE
# Student/Admin access
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
        return JSONResponse(status_code=404, content={"detail": "Utilisateur introuvable"})

    conversations = session.exec(
        select(models.Conversation)
        .where(models.Conversation.sso_id == sso_id)
        .order_by(models.Conversation.updated_at.desc())
    ).all()

    return {
        "ok": True,
        "conversations": [
            {
                "id": c.conversation_id,
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
        return JSONResponse(status_code=404, content={"detail": "Conversation introuvable"})

    messages = session.exec(
        select(models.Message)
        .where(models.Message.conversation_id == conversation_id)
        .order_by(models.Message.sent_at.asc())
    ).all()

    return {
        "ok": True,
        "conversation": {
            "id": conv.conversation_id,
            "title": conv.title,
        },
        "messages": [
            {"role": m.role, "content": m.content}
            for m in messages
        ],
    }


# ------------------------------------------------------------------
# Chat streaming endpoint
# Sauvegarde les messages en DB + historique 10 derniers messages
# ------------------------------------------------------------------


@app.post("/chat/stream")
async def chat_stream_endpoint(
    request: Request,
    data: ChatRequest,
    session: Session = Depends(get_session),
):
    user = get_current_user(request)

    if not user:
        return JSONResponse(status_code=401, content={"detail": "Non connecté"})

    sso_id = session.exec(
        select(models.User.sso_id).where(models.User.email == user["email"])
    ).first()

    now = datetime.utcnow()
    conversation_id = data.conversation_id

    # Nouvelle conversation si pas d'ID fourni
    if not conversation_id:
        conversation_id = str(uuid.uuid4())
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

    # Sauvegarde le message user
    user_msg = models.Message(
        message_id=str(uuid.uuid4()),
        role="user",
        content=data.message,
        sent_at=now,
        conversation_id=conversation_id,
    )
    session.add(user_msg)
    session.commit()

    # Récupère les 10 derniers messages pour le contexte Mistral
    all_messages = session.exec(
        select(models.Message)
        .where(models.Message.conversation_id == conversation_id)
        .order_by(models.Message.sent_at.asc())
    ).all()

    history = [
        {"role": m.role, "content": m.content}
        for m in all_messages[-10:]
    ]

    full_response = []

    def generate():
        for chunk in chat_stream_with_history(history):
            full_response.append(chunk)
            yield f"data: {chunk}\n\n"

        # Envoie le conversation_id au frontend
        yield f"data: [CONV_ID:{conversation_id}]\n\n"
        yield "data: [DONE]\n\n"

        # Sauvegarde la réponse assistant en DB
        assistant_msg = models.Message(
            message_id=str(uuid.uuid4()),
            role="assistant",
            content="".join(full_response),
            sent_at=datetime.utcnow(),
            conversation_id=conversation_id,
        )
        session.add(assistant_msg)

        # Met à jour updated_at de la conversation
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
# Chat reset (legacy)
# ------------------------------------------------------------------


@app.post("/chat/reset")
async def chat_reset_endpoint():

    reset_conversation()

    return {
        "ok": True,
        "message": "Conversation reinitialisee",
    }


# ------------------------------------------------------------------
# Chat complete response (legacy)
# ------------------------------------------------------------------


@app.post("/chat/complete")
async def chat_complete_endpoint(data: ChatRequest):

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
# QCM generation
# ------------------------------------------------------------------


@app.post("/generate_qcm")
async def generate_qcm_endpoint(data: QCMRequest):

    questions = []
    errors = []

    for i in range(data.n):

        try:

            qcm = generate_qcm_statement(
                notion=data.notion,
                niveau=data.niveau,
            )

            questions.append(qcm)

        except Exception as e:

            errors.append({
                "index": i,
                "error": str(e),
            })

    if not questions:

        return JSONResponse(
            status_code=500,
            content={
                "ok": False,
                "error": "Aucune question générée",
                "details": errors,
            },
        )

    return {
        "ok": True,
        "notion": data.notion,
        "niveau": data.niveau,
        "questions": questions,
        "errors": errors,
    }


# ------------------------------------------------------------------
# Run app
# ------------------------------------------------------------------


if __name__ == "__main__":

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
    )