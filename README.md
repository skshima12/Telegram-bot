# SK BOT HOSTING — Starter (Python + JavaScript)

This is a **starter MVP**, not a production public hosting platform. It includes:
- Telegram bot UI: Upload Code, Check Logs, Updates, Developer
- Admin broadcast (admin Telegram ID configured by environment)
- Per-Telegram-user project ownership enforced by the runner API
- Python `.py` and JavaScript `.js` scripts
- Optional `requirements.txt` or `package.json`
- Docker container per project with memory/CPU/PID limits and restricted container privileges
- Deploy / Stop / Restart / Delete / Logs

## Important architecture reality

- Vercel is suitable for a web dashboard or stateless API/webhook, not long-running user bots.
- The Telegram bot in this starter uses long polling and should run as a persistent worker.
- The runner requires a host with Docker Engine access. Many free PaaS tiers do not allow a Docker daemon or persistent containers, and free services may sleep. Therefore **a fully free, always-on public hosting service cannot be promised**.
- This starter is not safe enough for unrestricted public access without additional abuse controls, quotas, network egress controls, monitoring, and a security review. Do not expose the runner to the public internet without HTTPS and a strong secret.

## Folder structure

- `bot/` — Telegram bot (persistent Python process)
- `runner/` — FastAPI control API that creates isolated Docker containers
- `runner/Dockerfile` — runner API image (the host must also provide access to Docker Engine)
- `runner/worker-images/` — base images used for customer scripts
- `.env.example` — configuration template

## 1. Create a Telegram bot

1. Open `@BotFather` in Telegram.
2. Run `/newbot`, follow prompts, and copy the token.
3. Keep the token private. Do not paste it into code or GitHub.
4. Do not set a webhook for this polling version.

## 2. Configure environment variables

Copy `.env.example` to `.env` and fill in:
- `TELEGRAM_BOT_TOKEN`
- `RUNNER_URL` — URL of your runner API
- `RUNNER_API_SECRET` — same long random secret on bot and runner
- `ADMIN_ID=6416284194`

Generate a secret, for example:
`python -c "import secrets; print(secrets.token_urlsafe(48))"`

Never commit `.env`.

## 3. Run the runner on a Docker-capable host

A machine with Docker Engine is required. In `runner/`:
```bash
pip install -r requirements.txt
export RUNNER_API_SECRET="same-long-secret"
export DATA_DIR="./data"
uvicorn app:app --host 0.0.0.0 --port 8080
```
Use HTTPS via a reverse proxy before exposing it publicly. For a first local test, use `http://127.0.0.1:8080`.

The runner process needs Docker socket access. **This is a high-privilege capability**: anyone who steals the runner secret may control containers. Keep the API private where possible, use firewall rules, rotate secrets after exposure, and never mount the Docker socket into customer containers.

## 4. Run the Telegram bot

In another terminal, from `bot/`:
```bash
pip install -r requirements.txt
```
Set environment variables from `.env` in your hosting provider. Run:
```bash
python bot.py
```
The bot uses long polling, so run it as a persistent worker, not a Vercel function. If your provider sleeps free services, the bot may go offline.

## 5. Test flow

1. Start the runner.
2. Start the bot.
3. Open your bot and send `/start`.
4. Choose **Upload Code**, enter a project name, then upload a `.py` or `.js` file.
5. Optionally attach `requirements.txt` for Python or `package.json` for Node.js.
6. Deploy, then use **Check Logs**.
7. Test Stop, Restart, and Delete.

## Worker runtime behavior

- One container per project, non-root user, read-only source mount, read-only root filesystem, dropped Linux capabilities, no-new-privileges, memory/CPU/PID limits.
- `/tmp` is a bounded tmpfs for dependency installation/runtime scratch.
- Network is enabled so normal Telegram bots can connect to Telegram. This means network egress is **not fully sandboxed**. For public hosting, add outbound network filtering/allowlists and anti-abuse controls before launch.
- Users can run code that consumes resources within the container limits; enforce account quotas and rate limits before public launch.
- Only the uploaded source/dependency manifest is accepted. User-supplied Dockerfiles and archives are not accepted.

## Free-tier warning

Use free tiers for development/testing only. Always-on execution, Docker support, persistent storage, bandwidth, and resource limits depend on the provider and can change. Do not advertise unlimited or permanent free hosting unless you have verified the provider terms and capacity.

## Security warning

This starter is an MVP, not a hardened multi-tenant cloud. Before public launch, add:
- HTTPS, strict rate limits, request size limits at proxy, audit logs, quotas, abuse reporting, and automated cleanup
- outbound network policy / egress firewall
- per-user storage quotas and durable metadata backups
- dependency allowlisting or malware scanning and an acceptable-use policy
- monitoring, alerting, and incident response
- independent security review

## Troubleshooting

- `Runner unavailable`: check `RUNNER_URL`, runner process, firewall and shared secret.
- `Docker daemon unavailable`: install/start Docker Engine on the runner host and ensure the API process can access it.
- Python module errors: upload `requirements.txt` as an additional file before deployment.
- Node module errors: upload a valid `package.json` with dependencies.
- Bot does not respond: verify `TELEGRAM_BOT_TOKEN` and ensure only one polling instance is running.
