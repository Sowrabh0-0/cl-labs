# Clahan Labs VM Gateway

Lightweight VM access gateway:

- TypeScript frontend for login and session launch.
- FastAPI backend for authentication, VM mapping, and session state.
- Apache Guacamole remains responsible for the remote desktop stream.

## Current MVP Flow

1. First run creates an admin account through the UI.
2. Admin creates users and maps each user to a fixed VM.
3. User logs in through the TypeScript UI.
4. FastAPI validates the user from SQLite.
5. FastAPI returns the VM mapped to that user.
6. The UI shows the assigned VM and opens the configured Guacamole URL.

## Session Policy

- Max session duration: 4 hours.
- Idle timeout: 15 minutes.
- Reconnect window: available while the session is not idle-expired.

Credentials are stored in SQLite as PBKDF2 password hashes. They are not hardcoded in the codebase.

## What You Need To Provide

- Confirm the public Guacamole URL, including whether it is `/guacamole`.
- Confirm whether Guacamole already has a connection for `vm-vid-client1`.
- Provide the real Guacamole connection identifier or launch URL for each VM.
- Confirm whether the target VM should be reached by public IP for now or private VNet IP.
- Confirm the real Guacamole launch URL format for each connection.
- Confirm whether Guacamole should be embedded later or opened in a new tab.

## Run Backend

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Optional environment variables:

```bash
APP_SESSION_SECRET=replace-with-a-long-random-string
COOKIE_SECURE=false
FRONTEND_ORIGIN=http://localhost:5173
GUACAMOLE_PUBLIC_URL=/guacamole
```

## Run Frontend

```bash
cd frontend
npm install
npm run dev
```

The frontend defaults to `http://localhost:8000` for the backend.

The frontend intentionally avoids Vite/esbuild because some company laptops block helper process spawning and trigger `spawn EPERM`. The current setup uses TypeScript compiler output plus a tiny Node static server.

## Run With Docker Compose

On the Azure VM:

```bash
cp .env.example .env
```

Edit `.env`:

```bash
APP_BIND=127.0.0.1
APP_PORT=8088
APP_SESSION_SECRET=replace-with-a-long-random-string
COOKIE_SECURE=true
FRONTEND_ORIGIN=https://vdi.clahanlabs.com
GUACAMOLE_PUBLIC_URL=/guacamole
```

Start the stack:

```bash
docker compose up -d --build
```

Open:

```bash
https://vdi.clahanlabs.com
```

The Compose stack includes:

- `frontend`: Nginx serving the compiled TypeScript UI and proxying `/api` to FastAPI.
- `backend`: FastAPI app on the internal Docker network.
- `backend-data`: persistent SQLite volume for users, VM mappings, and sessions.

## Host Nginx With Local Guacamole

For the Azure VM deployment, Guacamole stays on the host at:

```bash
http://127.0.0.1:8080/guacamole/
```

The Docker app binds only to localhost:

```bash
http://127.0.0.1:8088/
```

Use [deploy/nginx/clahanlabs-vdi.conf](deploy/nginx/clahanlabs-vdi.conf) as the host Nginx config. It routes:

- `/` to the Docker app on `127.0.0.1:8088`
- `/api/` to the Docker app on `127.0.0.1:8088`
- `/guacamole/` to Guacamole on `127.0.0.1:8080/guacamole/`

Apply it on the Azure VM:

```bash
sudo cp deploy/nginx/clahanlabs-vdi.conf /etc/nginx/sites-available/clahanlabs-vdi
sudo nginx -t
sudo systemctl reload nginx
```

Your old config proxied `/` directly to Guacamole. This new config gives `/` to the application and moves Guacamole access to `/guacamole/`.

Useful commands:

```bash
docker compose logs -f
docker compose ps
docker compose down
```

Do not run `docker compose down -v` unless you intentionally want to delete the SQLite data volume.

To override:

```bash
VITE_API_BASE_URL=http://localhost:8000
```

## Files To Edit First

- `backend/data/seed.json`: initial VM registration only.
- `backend/.env.example`: copy values into your environment for deployment.
- `decisions.md`: architecture decisions and next steps.
- `your-brain.md`: durable project context for future work.
