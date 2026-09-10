# MATHutrice

MATHutrice is an educational tutor powered by an LLM, built to help first-year students learn and practice mathematical tools. It generates exercises (QCM, QRO, step-by-step) tailored to a student's actual progress, verifies answers mathematically instead of trusting the LLM blindly, and tracks skill mastery notion by notion.

## Features

- **Adaptive exercise generation** — QCM, QRO and step-by-step questions generated via Mistral AI, with answers verified with SymPy rather than taken on faith from the LLM.
- **Skill tracking** — every competence is scored per student (0–1) and persisted after each answer; a student's level on a notion (basique / solide / expert) is deduced automatically from those scores.
- **Positioning & training sessions** — a first-time positioning test spans all three levels of a notion; afterwards, training serves one adaptive question at a time based on the student's current level.
- **Student-facing front-end** — server-rendered pages (Jinja2): a home page and a per-module page showing a short description of the notion, a global progression bar, skills evaluated (aligned with Moodle/EPF), a quick "M'entraîner" button, and progression broken down by subtheme.
- **Authentication** — Microsoft/Azure AD sign-in (MSAL), with a teacher view and a student-impersonation mode for support.

This is a visual mockup of the home page:
<img width="934" height="522" alt="image" src="https://github.com/user-attachments/assets/faa9c372-129c-4414-9160-f5f263b10f12" />

## Tech stack

- **API** — FastAPI + Starlette, server-rendered with Jinja2 templates
- **Database** — PostgreSQL via SQLModel / psycopg2
- **LLM** — Mistral AI (`mistralai` SDK), with math answer verification via SymPy
- **Auth** — Microsoft Authentication Library (MSAL), session cookies via Starlette's `SessionMiddleware`
- **Scheduling** — APScheduler (background cleanup of stale conversations)

## Getting started

### Prerequisites
- Python 3.12+
- A PostgreSQL database (the team develops against a Postgres container running on an EPF-provided VM, alongside Adminer and VS Code)

### Install
```bash
pip install -r requirements.txt
```

### Configure
Set these environment variables (a `.env` file at the repo root is loaded automatically):

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | PostgreSQL connection string |
| `SESSION_SECRET` | Secret used to sign session cookies |
| `CLIENT_ID`, `CLIENT_SECRET`, `TENANT_ID` | Azure AD app registration (MSAL sign-in) |
| `MISTRAL_API_KEY` | Mistral AI API key used for exercise generation |

### Run
```bash
cd generator_test
uvicorn app:app --reload
```

### Test
Unit tests use `pytest` and live next to the module they cover, e.g.:
```bash
pytest generator_test/fonctions_python/test_session_generator.py
```

## Project structure

```
generator_test/
├── app.py                    # FastAPI app: auth, pages, session/training endpoints
├── database.py               # SQLModel engine/session
├── models.py                 # SQLModel tables (User, Notion, Competence, Progression, ...)
├── fonctions_python/
│   ├── main.py                  # REFERENTIEL (notions/competences catalogue) + question dispatch
│   ├── session_generator.py     # positioning/training session generation, score persistence
│   ├── base_generator.py        # Mistral client, prompt/response plumbing
│   └── type_questions/          # QCM / QRO / step-by-step generators
├── templates/, static/       # Jinja2 front-end
└── tests/                    # pytest suite
```

## Contributing

- Issues are tracked as GitHub Issues in this repo (see `docs/agents/issue-tracker.md`).
- Work happens on feature branches merged via pull request — direct pushes to `main` are blocked.
- Domain and architecture notes live under `docs/` — see `docs/MATHutrice-notes/README.md` for the referentiel architecture and `docs/agent-control.md` for the agent guardrails in place on this repo.
