# Project Brain

Last updated: 2026-06-17

## Current Goal

Build a lightweight web application where users log in with username/password, are mapped to an Azure VM, and then access a stable browser-based session to that VM.

The app should have two main pages:

- Auth / VM resolution page.
- Actual remote VM session page.

The session must be low latency and should avoid unnecessary interruption.

## Architecture Context

Reference image: `image.png`

Observed architecture from the image:

- Browser connects over HTTPS.
- DNS / SSL routes to Nginx.
- Nginx terminates SSL.
- Nginx proxies to Apache Guacamole running on an Azure Ubuntu VM.
- Guacamole web app runs on Tomcat `:8080`.
- Guacamole talks to `guacd`.
- `guacd` translates browser sessions to RDP/VNC/SSH.
- Target Linux VMs run Ubuntu/xRDP.
- Target VMs should ideally be inside an Azure VNet private subnet.

Known test machines from `machines.md`:

- Guacamole VM public IP: `135.235.218.233`
- Test client VM public IP: `4.186.72.63`
- Test client VM private IP observed from logs: `10.10.2.4`

Note: `prompt.md` was referenced by the IDE tab list but was not present in the workspace when inspected.

## Key Decision Link

Primary decision document: `decisions.md`

The recommendation in `decisions.md` is to use:

- FastAPI as the control plane.
- Apache Guacamole as the session/data plane.
- Lightweight UI using TypeScript static build for MVP.
- Manual VM registration first, followed by Azure SDK automation later.

## Current Scaffold

Created first application scaffold:

- `backend/app/main.py`: FastAPI app with login, cookie session, session lookup, and logout.
- `backend/data/seed.json`: initial VM registration only.
- `frontend/src/main.ts`: lightweight TypeScript UI for setup, login, session page, and admin dashboard.
- `frontend/src/styles.css`: responsive UI styling.
- `README.md`: run instructions and required inputs from the user.

## Confirmed Session Policy

- Maximum session duration: 4 hours.
- Idle timeout: 15 minutes.
- Reconnect is allowed only while the idle timeout has not been reached.

## Auth And Mapping Direction

Credentials and user-to-VM mappings should be stored in SQLite for now, not hardcoded in the codebase. Admin users can create users, mark users as admins, register VMs, and map users to fixed VMs.

Guacamole direct client URLs should not be guessed from VM names. The app should open `/guacamole/` by default unless an admin provides a known-good Guacamole launch URL.

Guacamole DB sync is now planned/implemented in the backend: creating/resetting an app user can create a matching Guacamole user with the same credentials, create/update the Guacamole connection, and grant connection permission. Regular VMs should be assigned to only one non-admin user at a time.

Credential split:

- App password authenticates to the Clahan Labs app and to the synced Guacamole user.
- VM/RDP password authenticates to the target VM and is synced as a Guacamole connection parameter when a VM is registered.
- Do not assume the app user password and VM password are the same.

## Deployment Direction

Docker Compose has been added for Azure VM deployment:

- `backend/Dockerfile`
- `frontend/Dockerfile`
- `frontend/nginx.conf`
- `docker-compose.yml`
- root `.env.example`

The frontend container uses Nginx to serve static files and proxy `/api` to the backend service. Backend SQLite data is stored in the `backend-data` named volume.

Guacamole has moved into Docker Compose because host Tomcat 10 produced Guacamole API 500s and servlet compatibility errors. The desired host Nginx routing is:

- `/` -> Docker app on `127.0.0.1:8088`
- `/api/` -> Docker app on `127.0.0.1:8088`
- `/guacamole/` -> Docker Guacamole on `127.0.0.1:8090/guacamole/`

## Working Mental Model

FastAPI should decide who the user is and which VM they get. It should not carry the remote desktop stream.

Guacamole should carry the latency-sensitive session stream. This keeps the system simpler, faster, and more reliable.

The first MVP should prove:

1. A manually created Azure VM can be reached from Guacamole.
2. A user can log in.
3. The user is mapped to one VM.
4. The user lands on a session page.
5. The session opens through Guacamole.
6. Reload/reconnect behavior is acceptable.

## Future Things To Remember

- Active Guacamole sessions are stateful. Restarting `guacd` can drop sessions.
- If scaling Guacamole later, design sticky routing and session placement carefully.
- Target VMs should move behind private networking instead of public IP exposure.
- Prefer the target VM private IP for Guacamole connections when both VMs share a VNet. For `vm-vid-client1`, try `10.10.2.4:3389` from `vm-guacamole`.
- Store VM credentials securely, eventually in Azure Key Vault.
- Consider OIDC/SSO later, but username/password is acceptable for the MVP.
