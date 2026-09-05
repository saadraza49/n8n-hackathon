# FastAPI Authentication Backend for n8n

A minimal, production-ready, hackathon-friendly FastAPI authentication service built specifically to be consumed from **n8n HTTP Request nodes** and deployed to **Railway** or any modern cloud platform.

---

## 1. Project Folder Structure

```
n8n Hackathon 2026/
│
├── app/
│   ├── __init__.py
│   ├── main.py                 # FastAPI app, CORS setup, /health & route registration
│   ├── database.py             # SQLAlchemy engine, session maker, get_db dependency
│   ├── core/
│   │   ├── __init__.py
│   │   ├── config.py           # Pydantic Settings loading environment variables (.env)
│   │   └── security.py         # Bcrypt password hashing & python-jose JWT management
│   ├── models/
│   │   ├── __init__.py
│   │   └── user.py             # SQLAlchemy User model (users table)
│   ├── schemas/
│   │   ├── __init__.py
│   │   └── auth.py             # Pydantic request & response validation schemas
│   ├── dependencies/
│   │   ├── __init__.py
│   │   └── auth.py             # get_current_user JWT dependency
│   └── routers/
│       ├── __init__.py
│       └── auth.py             # Endpoints: /auth/signup, /auth/login, /auth/me
│
├── alembic/
│   ├── env.py                  # Alembic environment connected to app config and models
│   ├── script.py.mako          # Alembic migration script template
│   └── versions/
│       └── 0001_create_users_table.py  # Initial migration for users table
│
├── tests/
│   ├── __init__.py
│   ├── conftest.py             # Pytest configuration with isolated SQLite test DB
│   └── test_auth.py            # Automated tests for signup, login, auth/me, and edge cases
│
├── .env.example                # Safe template for environment variables
├── .gitignore                  # Git ignore rules for Python, virtualenv, and secrets
├── alembic.ini                 # Alembic configuration
├── Procfile                    # Production startup command for Railway
├── README.md                   # Complete documentation and n8n guide
└── requirements.txt            # Minimal, production-ready dependencies
```

---

## 2. Database Schema

The database contains **only one table**: `users`.

| Column | Type | Constraints / Details |
| :--- | :--- | :--- |
| `id` | `UUID` | Primary Key, default `uuid.uuid4` |
| `name` | `VARCHAR(255)` | Not Null |
| `email` | `VARCHAR(255)` | Unique, Indexed, Not Null |
| `password_hash` | `VARCHAR(255)` | Bcrypt hash, Not Null (Never exposed in API) |
| `created_at` | `TIMESTAMP WITH TIME ZONE` | Default `now()`, Not Null |
| `updated_at` | `TIMESTAMP WITH TIME ZONE` | Default `now()`, updates on row change |

---

## 3. Installation & Local Setup

### Prerequisites
- Python 3.10+ (tested on Python 3.12, 3.13, 3.14)
- PostgreSQL database (e.g. Supabase, Neon, Railway Postgres, or local PostgreSQL)

### Step 1: Clone or Navigate to the Directory
```bash
cd "n8n Hackathon 2026"
```

### Step 2: (Recommended) Create & Activate a Virtual Environment
**On Windows (PowerShell / Command Prompt):**
```powershell
python -m venv venv
.\venv\Scripts\activate
```

**On macOS / Linux:**
```bash
python3 -m venv venv
source venv/bin/activate
```

### Step 3: Install Dependencies
```bash
pip install -r requirements.txt
```

---

## 4. Environment Configuration (`.env`)

Create a `.env` file in the root folder by copying `.env.example`:

```bash
cp .env.example .env
```

Set your values inside `.env`:

```env
# PostgreSQL connection string (Supabase / Railway / Local)
DATABASE_URL=postgresql://user:password@host:5432/postgres

# Secret key for signing JWT tokens (generate a 32-byte hex string)
SECRET_KEY=your_generated_random_secret_key_here

# JWT Algorithm (default: HS256)
ALGORITHM=HS256

# JWT Expiration in minutes (default: 60)
ACCESS_TOKEN_EXPIRE_MINUTES=60

# Port (default: 8000, dynamically read by Railway)
PORT=8000
```

> **Generating a Secure Secret Key:**
> Run this one-liner in your terminal to generate a 32-byte random key:
> ```bash
> python -c "import secrets; print(secrets.token_hex(32))"
> ```

---

## 5. Running Database Migrations

Apply the Alembic migrations to create the `users` table:

```bash
python -m alembic upgrade head
```

---

## 6. Running the Application Locally

Start the development server with Uvicorn:

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

The API will be available at:
- **Base URL:** `http://127.0.0.1:8000`
- **Swagger UI (Interactive API Docs):** `http://127.0.0.1:8000/docs`
- **ReDoc:** `http://127.0.0.1:8000/redoc`
- **Health Check:** `http://127.0.0.1:8000/health`

---

## 7. Testing with Swagger UI (`/docs`)

1. Open `http://127.0.0.1:8000/docs` in your browser.
2. **Signup:**
   - Open `POST /auth/signup`.
   - Click **Try it out**.
   - Enter:
     ```json
     {
       "name": "Bruce Wayne",
       "email": "bruce@wayne-enterprises.com",
       "password": "batman_secret_password"
     }
     ```
   - Click **Execute**. You will receive a `201 Created` response containing the user UUID, name, email, and timestamps (no password hash).
3. **Login:**
   - Open `POST /auth/login`.
   - Click **Try it out**.
   - Enter:
     ```json
     {
       "email": "bruce@wayne-enterprises.com",
       "password": "batman_secret_password"
     }
     ```
   - Click **Execute**. You will receive a `200 OK` response with:
     ```json
     {
       "access_token": "eyJhbGciOi...",
       "token_type": "bearer"
     }
     ```
   - Copy the `access_token` string.
4. **Authorize in Swagger:**
   - Scroll up and click the green **Authorize** button at the top right of the Swagger UI.
   - Enter `Bearer <YOUR_TOKEN>` or paste just the token.
   - Click **Authorize**, then **Close**.
5. **Get Current User:**
   - Open `GET /auth/me`.
   - Click **Try it out** -> **Execute**.
   - You will receive a `200 OK` response containing Bruce Wayne's user profile.

---

## 8. Consuming Endpoints in n8n (HTTP Request Node)

This API was engineered specifically for easy consumption from **n8n HTTP Request nodes**. Every endpoint accepts and returns clean JSON without browser-specific cookies or complex form-encoding.

### Workflow Example in n8n:
```
[ Trigger / Webhook ] ──▶ [ HTTP Request: Login ] ──▶ [ HTTP Request: Protected Call (/auth/me) ]
```

---

### Endpoint 1: Health Check
- **Method:** `GET`
- **URL:** `{{ $env.API_BASE_URL }}/health`
- **Headers:** None required
- **Response (`200 OK`):**
  ```json
  {
    "status": "ok"
  }
  ```

---

### Endpoint 2: User Registration (`/auth/signup`)
- **Method:** `POST`
- **URL:** `{{ $env.API_BASE_URL }}/auth/signup`
- **Headers:**
  - `Content-Type: application/json`
- **Request Body (JSON):**
  ```json
  {
    "name": "Jane Doe",
    "email": "jane@example.com",
    "password": "strongPassword123"
  }
  ```
- **Response (`201 Created`):**
  ```json
  {
    "id": "e4b9d0b6-1934-4b5c-a5b7-7e9bbfdbf211",
    "name": "Jane Doe",
    "email": "jane@example.com",
    "created_at": "2026-09-05T14:26:58.264766Z",
    "updated_at": "2026-09-05T14:26:58.264766Z"
  }
  ```
- **Error Response (`400 Bad Request`):**
  ```json
  {
    "detail": "Email is already registered"
  }
  ```

---

### Endpoint 3: User Login (`/auth/login`)
- **Method:** `POST`
- **URL:** `{{ $env.API_BASE_URL }}/auth/login`
- **Headers:**
  - `Content-Type: application/json`
- **Request Body (JSON):**
  ```json
  {
    "email": "jane@example.com",
    "password": "strongPassword123"
  }
  ```
- **Response (`200 OK`):**
  ```json
  {
    "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
    "token_type": "bearer"
  }
  ```
- **Error Response (`401 Unauthorized`):**
  ```json
  {
    "detail": "Invalid email or password"
  }
  ```

---

### Endpoint 4: Protected User Profile (`/auth/me`)
- **Method:** `GET`
- **URL:** `{{ $env.API_BASE_URL }}/auth/me`
- **Authentication in n8n HTTP Request Node:**
  - **Option A (Generic Header):**
    - Header Name: `Authorization`
    - Header Value: `Bearer {{ $json.access_token }}`
  - **Option B (Authentication Dropdown):**
    - Select **Header Auth** or **Generic Credential Type** -> `Authorization: Bearer <TOKEN>`
- **Response (`200 OK`):**
  ```json
  {
    "id": "e4b9d0b6-1934-4b5c-a5b7-7e9bbfdbf211",
    "name": "Jane Doe",
    "email": "jane@example.com",
    "created_at": "2026-09-05T14:26:58.264766Z",
    "updated_at": "2026-09-05T14:26:58.264766Z"
  }
  ```
- **Error Response (`401 Unauthorized`):**
  ```json
  {
    "detail": "Could not validate credentials"
  }
  ```

---

## 9. Running Automated Tests

Run the complete test suite with `pytest`:

```bash
python -m pytest tests/ -v
```

All tests run against an isolated in-memory SQLite database and test all endpoints, input validation, duplicate email prevention, invalid credentials handling, and JWT expiration.

---

## 10. Deploying to Railway

The project is pre-configured for instant deployment on [Railway](https://railway.app):

1. **Push your code to GitHub:**
   ```bash
   git init
   git add .
   git commit -m "feat: initial fastapi authentication backend"
   git branch -M main
   git remote add origin <your-github-repo-url>
   git push -u origin main
   ```
2. **Create a New Project on Railway:**
   - Go to [railway.app](https://railway.app) and sign in.
   - Click **New Project** -> **Deploy from GitHub repo**.
   - Select your repository.
3. **Add a PostgreSQL Database (if not using external Supabase):**
   - Click **+ New** -> **Database** -> **Add PostgreSQL**.
   - Railway automatically provides the `DATABASE_URL` variable to your service.
4. **Configure Environment Variables in Railway:**
   - Click on your backend service -> **Variables** tab.
   - Set:
     - `DATABASE_URL`: Your Supabase connection string OR the Railway PostgreSQL variable `${{Postgres.DATABASE_URL}}`.
     - `SECRET_KEY`: A secure 32-byte hex key (generate with `python -c "import secrets; print(secrets.token_hex(32))"`).
     - `ALGORITHM`: `HS256`
     - `ACCESS_TOKEN_EXPIRE_MINUTES`: `60`
5. **Start Command:**
   - Railway automatically detects the included `Procfile`:
     ```
     web: uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
     ```
   - (Optional) You can set Railway's **Custom Build Command** or **Pre-deploy Command** to run migrations:
     ```bash
     python -m alembic upgrade head
     ```
6. **Generate Domain:**
   - In your Railway service settings, click **Generate Domain** under **Networking**.
   - You will receive a public HTTPS URL (e.g., `https://your-service.up.railway.app`).
   - Use this URL inside your n8n workflows!
