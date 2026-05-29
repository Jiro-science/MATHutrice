from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, HTMLResponse
from starlette.middleware.sessions import SessionMiddleware

from dotenv import load_dotenv
import msal
import os
import uuid
import json

load_dotenv()

app = FastAPI()

app.add_middleware(
    SessionMiddleware,
    secret_key="test-secret-temporaire"
)

CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
TENANT_ID = os.getenv("TENANT_ID")

REDIRECT_URL = "https://mathutrice-preprod.mde.epf.fr/auth"
SCOPE = ["User.Read"]
AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"


def get_msal_app():
    return msal.ConfidentialClientApplication(
        CLIENT_ID,
        authority=AUTHORITY,
        client_credential=CLIENT_SECRET,
    )


# -------------------------
# LOGIN
# -------------------------
@app.get("/login")
async def login(request: Request):

    state = str(uuid.uuid4())
    request.session["state"] = state

    msal_app = get_msal_app()

    auth_url = msal_app.get_authorization_request_url(
        scopes=SCOPE,
        redirect_uri=REDIRECT_URL,
        state=state,
    )

    return RedirectResponse(auth_url)


# -------------------------
# CALLBACK (TON CODE INCHANGÉ)
# -------------------------
@app.get("/auth")
async def callback(request: Request, code: str = None, error: str = None):

    if error:
        return HTMLResponse(
            f"<h2>❌ Erreur Azure</h2><pre>{error}</pre>"
        )

    msal_app = get_msal_app()

    result = msal_app.acquire_token_by_authorization_code(
        code,
        scopes=SCOPE,
        redirect_uri=REDIRECT_URL,
    )

    print("\n========== RESULT ==========")
    print(json.dumps(result, indent=2))
    print("===========================\n")

    if "error" in result:
        return HTMLResponse(f"""
            <h2>❌ Erreur token</h2>
            <pre>{json.dumps(result, indent=2)}</pre>
        """)

    claims = result.get("id_token_claims", {})

    email = claims.get("preferred_username")

    name = claims.get("name", "")

    # email = claims.get("email") or claims.get("preferred_username")

    if not email or not email.endswith("@epfedu.fr"):
        return HTMLResponse(
            """
            <h2>⛔ Accès refusé</h2>
            <p>Adresse EPF obligatoire.</p>
            """,
            status_code=403
        )

    roles = claims.get("roles") or ["Aucun role"]
    groups = claims.get("groups") or ["Aucun groupe"]

    # 💾 session normale
    request.session["user"] = {
        "email": email,
        "name": name,
        "impersonate": False
    }

    # 🔥 AJOUT BOUTONS SANS TOUCHER TON AFFICHAGE
    return HTMLResponse(f"""
        <h2>✅ Connexion réussie</h2>

        <h3>👤 Utilisateur</h3>
        <pre>
Nom   : {name}
Email : {email}
firstname : {firstname}
lastname : {lastname}
        </pre>

        <h3>🔐 Rôles</h3>
        <pre>{json.dumps(roles, indent=2)}</pre>

        <h3>👥 Groupes</h3>
        <pre>{json.dumps(groups, indent=2)}</pre>

        <h3>📋 Claims</h3>
        <pre>{json.dumps(claims, indent=2)}</pre>

        <hr>

        <a href="/logout">
            <button>logout</button>
        </a>

        <a href="/impersonate/franklin.kalfadangpalai@epfedu.fr">
            <button>impersonate</button>
        </a>
    """)


# -------------------------
# LOGOUT
# -------------------------
@app.get("/logout")
async def logout(request: Request):

    request.session.clear()

    return HTMLResponse("""
        <h2>👋 Vous êtes déconnecté</h2>

        <p>Votre session a été supprimée.</p>

        <a href="/login">
            <button>login</button>
        </a>
    """)


# -------------------------
# IMPERSONATE
# -------------------------
@app.get("/impersonate/{email}")
async def impersonate(request: Request, email: str):

    request.session["user"] = {
        "email": email,
        "name": "Impersonated User",
        "impersonate": True
    }

    return HTMLResponse(f"""
        <h2>🧪 Impersonation active</h2>

        <p><b>Email :</b> {email}</p>

        <hr>

        <a href="/logout">
            <button>logout</button>
        </a>

        <a href="/login">
            <button>login</button>
        </a>
    """)


    

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
    