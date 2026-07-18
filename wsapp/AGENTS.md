
You are operating as root on a production Debian VPS.

Your task is to inspect, fix, configure, deploy, test, and document this Django project completely.

## Project information

* Current repository directory: `/root/wsapp/wsapp`
* Domain: `hellens.schoolsoft.online`
* Server IP: `109.207.79.50`
* Django project name appears to be `config`, but inspect the source code and confirm the correct WSGI module.
* Required systemd service name: `wsapp.service`
* Required Nginx configuration filename: `wsapp`
* PostgreSQL must be used.
* Gunicorn must bind to a TCP loopback address such as `127.0.0.1:PORT`.
* Do not use a Unix socket.
* Nginx and other applications are already running on this VPS.
* Other applications and PostgreSQL databases must not be modified, restarted unnecessarily, overwritten, deleted, or disrupted.
* Do not introduce Celery or Redis. This project should run using normal Django, Gunicorn, PostgreSQL, Python requests, and Nginx unless the existing source code absolutely requires something else. Remove or fix accidental Celery/Redis startup dependencies if they prevent the application from working.

## Autonomous execution

Complete the deployment without pausing to ask me questions or waiting for confirmation.

Make reasonable, safe production decisions yourself.

Use non-interactive commands wherever possible. However, do not perform destructive operations. Never drop databases, remove unrelated Nginx files, stop unrelated services, overwrite unrelated systemd services, or kill unrelated processes.

Before modifying an existing project-specific configuration file, create a timestamped backup.

Do not simply provide instructions. Execute the work on the server.

## Phase 1: Deep project inspection

First inspect the entire project, including:

* `README.md`
* `IMPLEMENTATION_PLAN.md`
* `requirements.txt`
* `manage.py`
* `config/settings.py` or split settings files
* `config/urls.py`
* `config/wsgi.py`
* all Django applications
* templates and static files
* deployment-related scripts
* `.gitignore`
* `whatsapp_api.md`
* environment variable usage
* database configuration
* messaging/sending implementation
* imports and dependencies
* migrations
* tests

Determine:

1. The correct Django settings module.
2. The correct WSGI application path.
3. The Python version required.
4. Whether any environment variables are missing.
5. Whether the project currently assumes SQLite.
6. Whether the project contains broken imports, incomplete migrations, missing dependencies, invalid URLs, template errors, or production configuration problems.
7. Whether any code still assumes Redis or Celery.
8. How the WhatsApp API credentials are currently loaded.
9. Which URL can be used as a reliable application smoke test.

Run appropriate checks and tests. Fix application errors that prevent deployment.

At minimum run:

```bash
python manage.py check
python manage.py check --deploy
python manage.py showmigrations
```

Run the existing automated tests if they are present and practical. Fix deployment-blocking failures.

Do not expose WhatsApp tokens, PostgreSQL passwords, Django secret keys, or API credentials in terminal output, documentation, Git commits, service files readable by everyone, or command history unnecessarily.

## Phase 2: Safe production directory

Deploy the production application under:

```text
/var/www/wsapp
```

Keep `/root/wsapp/wsapp` as the source repository.

Use a safe synchronization method such as `rsync` to copy the application into `/var/www/wsapp`, excluding:

* `.git`
* existing virtual environments
* `__pycache__`
* `.pyc` files
* local SQLite databases
* temporary files
* collected static files
* uploaded files that should not be overwritten during later deployments

Do not delete or modify any other `/var/www` application.

Create a dedicated system user and group named `wsapp` when safe and appropriate. The service should not run as root.

Set ownership and permissions so that:

* the `wsapp` service can read the application;
* the application can write only to directories that genuinely require writing, such as media or logs;
* Nginx can read static and uploaded media files;
* secret environment files are not publicly readable.

## Phase 3: Virtual environment and dependencies

Create the virtual environment at:

```text
/var/www/wsapp/venv
```

If the repository `.gitignore` does not already ignore virtual environments, update it to include at least:

```gitignore
venv/
env/
.env
*.env
__pycache__/
*.py[cod]
staticfiles/
media/
*.sqlite3
```

Do not remove valid existing `.gitignore` entries.

Install dependencies from `requirements.txt`.

If production-required packages such as Gunicorn, PostgreSQL drivers, or environment-variable libraries are missing, add appropriate compatible packages to `requirements.txt` and install them.

Prefer a maintained PostgreSQL driver compatible with the installed Django and Python versions.

Do not install Redis or Celery merely for deployment.

## Phase 4: Secrets and environment configuration

Create a protected environment file:

```text
/etc/wsapp/wsapp.env
```

Create `/etc/wsapp` if necessary.

Set permissions to:

```text
root:wsapp
0640
```

The environment file should contain production settings such as:

* `DJANGO_SETTINGS_MODULE`
* `DJANGO_SECRET_KEY`
* `DJANGO_DEBUG=False`
* PostgreSQL database name
* PostgreSQL user
* PostgreSQL password
* PostgreSQL host
* PostgreSQL port
* allowed hosts
* CSRF trusted origins
* WhatsApp/API credentials required by the application
* any other settings discovered during source inspection

Generate strong secrets where required.

Use:

```text
hellens.schoolsoft.online
109.207.79.50
localhost
127.0.0.1
```

as appropriate allowed hosts.

Ensure this trusted origin is configured:

```text
https://hellens.schoolsoft.online
```

Inspect `whatsapp_api.md` carefully. If it contains credentials needed by the application, transfer them securely into `/etc/wsapp/wsapp.env` without printing their values. Modify the application to read credentials from environment variables rather than hard-coded source files.

Do not include secret values in `DEPLOYMENT.md`.

Do not delete `whatsapp_api.md` unless absolutely necessary. Never expose its secret contents in the final report.

## Phase 5: PostgreSQL

Inspect existing PostgreSQL databases and roles before creating anything.

Create project-specific PostgreSQL resources only:

```text
Database: wsapp_db
Role: wsapp_user
```

If either name already exists, inspect it before deciding whether it belongs to this application. Do not overwrite it blindly.

Generate a strong unique database password and store it only in `/etc/wsapp/wsapp.env`.

Grant `wsapp_user` privileges only on `wsapp_db`.

Make `wsapp_user` the appropriate owner of the application database and public schema where required by the installed PostgreSQL version.

Do not modify, rename, drop, migrate, or grant access to databases belonging to other applications.

Update Django database settings to use environment variables and PostgreSQL.

If the project currently contains a SQLite database with meaningful existing data, inspect it before migrating. Preserve it as a backup. Do not silently discard meaningful data.

Run migrations against only `wsapp_db`.

## Phase 6: Django production settings

Configure production-safe Django settings.

Requirements include:

```python
DEBUG = False
```

Configure correctly:

* `ALLOWED_HOSTS`
* `CSRF_TRUSTED_ORIGINS`
* PostgreSQL database settings
* `STATIC_URL`
* `STATIC_ROOT`
* `MEDIA_URL`
* `MEDIA_ROOT`
* secure proxy SSL header
* secure cookies after HTTPS is working
* sensible logging
* timezone already intended by the project
* correct template directories
* correct environment-variable loading

Recommended paths:

```text
STATIC_ROOT=/var/www/wsapp/staticfiles
MEDIA_ROOT=/var/www/wsapp/media
```

Because Nginx terminates HTTPS, configure Django appropriately for forwarded HTTPS requests, including:

```python
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
```

Avoid enabling settings that create redirect loops before Nginx and Certbot are configured.

Make configuration changes compatible with local management commands and production systemd execution.

Run:

```bash
python manage.py makemigrations --check
python manage.py migrate --noinput
python manage.py collectstatic --noinput
python manage.py check
```

Do not create unnecessary migrations merely because formatting or unrelated files changed.

If the application needs an initial superuser and none exists, do not invent a publicly known password. Document the safe command for creating one later.

## Phase 7: Gunicorn and free TCP port

Gunicorn must use TCP, not a Unix socket.

Inspect currently listening ports and existing service files:

```bash
ss -ltnp
systemctl list-units --type=service --state=running
```

Prefer:

```text
127.0.0.1:8099
```

If port `8099` is occupied, select the next suitable unused loopback port, such as `8100`, `8101`, and so on.

Do not stop or reconfigure the process currently occupying another application's port.

Create:

```text
/etc/systemd/system/wsapp.service
```

The service must:

* be named exactly `wsapp.service`;
* run as the dedicated `wsapp` user;
* use `/var/www/wsapp` as its working directory;
* load `/etc/wsapp/wsapp.env`;
* execute Gunicorn from `/var/www/wsapp/venv/bin/gunicorn`;
* use the actual WSGI module discovered from the project;
* bind to `127.0.0.1:<selected-free-port>`;
* use approximately 3 workers unless the server resources justify a safer number;
* use a timeout of approximately 120 seconds;
* restart automatically on failures;
* start after the network and PostgreSQL;
* use safe systemd hardening that does not prevent required media/static access;
* send logs to journald.

Example structure, adjusted to match the actual project:

```ini
[Unit]
Description=WSApp Django Gunicorn Service
After=network.target postgresql.service
Wants=postgresql.service

[Service]
Type=simple
User=wsapp
Group=wsapp
WorkingDirectory=/var/www/wsapp
EnvironmentFile=/etc/wsapp/wsapp.env
ExecStart=/var/www/wsapp/venv/bin/gunicorn \
    --workers 3 \
    --bind 127.0.0.1:8099 \
    --timeout 120 \
    --access-logfile - \
    --error-logfile - \
    config.wsgi:application
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Do not blindly use `config.wsgi:application`; confirm the actual WSGI module.

After creating the service:

```bash
systemctl daemon-reload
systemctl enable wsapp.service
systemctl restart wsapp.service
systemctl status wsapp.service --no-pager
journalctl -u wsapp.service -n 100 --no-pager
```

Resolve all startup errors.

Test Gunicorn directly through its loopback port before configuring Nginx:

```bash
curl -I http://127.0.0.1:<selected-port>/
```

Use another known valid application route if `/` is not implemented.

## Phase 8: Nginx

Create the Nginx configuration file exactly at:

```text
/etc/nginx/sites-available/wsapp
```

Enable it with:

```text
/etc/nginx/sites-enabled/wsapp
```

Do not overwrite or modify unrelated Nginx configuration files.

Before creating the configuration:

* inspect existing `server_name` entries;
* verify that `hellens.schoolsoft.online` is not already assigned elsewhere;
* inspect currently enabled sites;
* back up an existing project-specific `wsapp` file if one exists.

Configure:

* `server_name hellens.schoolsoft.online`;
* reverse proxy to `http://127.0.0.1:<selected-port>`;
* static files from `/var/www/wsapp/staticfiles/`;
* media files from `/var/www/wsapp/media/`;
* forwarded host, real IP, protocol, and port headers;
* reasonable upload size;
* proxy timeouts appropriate for sending campaigns;
* no exposure of secret files;
* no directory listing.

Use headers similar to:

```nginx
proxy_set_header Host $host;
proxy_set_header X-Real-IP $remote_addr;
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
```

Use suitable longer proxy timeouts if the application performs message-sending operations in a normal Django request, while avoiding unlimited timeouts.

Before reloading Nginx, always run:

```bash
nginx -t
```

Only reload Nginx when the configuration test succeeds:

```bash
systemctl reload nginx
```

Do not restart Nginx unless reload is insufficient.

Test:

```bash
curl -I -H "Host: hellens.schoolsoft.online" http://127.0.0.1/
curl -I http://hellens.schoolsoft.online/
```

Resolve `400`, `403`, `404`, `500`, `502`, redirect-loop, static-file, CSRF, and host-header issues caused by the deployment.

A deliberate application-level `404` is acceptable only when no root URL exists; in that case test a real route and document it.

## Phase 9: DNS and HTTPS

Verify DNS before requesting the certificate:

```bash
getent hosts hellens.schoolsoft.online
```

Confirm that it resolves to:

```text
109.207.79.50
```

Confirm ports 80 and 443 are not blocked by the local firewall.

Use the existing Certbot installation if available. If Certbot is missing, install the Debian-supported Certbot Nginx package non-interactively.

Obtain and install a certificate for:

```text
hellens.schoolsoft.online
```

Use the Nginx plugin and enable HTTP-to-HTTPS redirection.

Do not request certificates for unrelated domains.

Use the server's existing Certbot account configuration when available. If no Certbot account exists and an email is required, register non-interactively without an email rather than pausing to ask me.

Example command, adjusted as required:

```bash
certbot --nginx \
  -d hellens.schoolsoft.online \
  --non-interactive \
  --agree-tos \
  --redirect \
  --register-unsafely-without-email
```

Do not repeatedly request certificates if one already exists and is valid.

After Certbot finishes, run:

```bash
nginx -t
systemctl reload nginx
certbot certificates
systemctl status certbot.timer --no-pager
```

Test:

```bash
curl -I https://hellens.schoolsoft.online/
```

Confirm that plain HTTP redirects to HTTPS.

## Phase 10: Full application testing

Verify all of the following:

1. `wsapp.service` is active and enabled.
2. Gunicorn listens only on the chosen `127.0.0.1` TCP port.
3. Nginx proxies the domain to the correct port.
4. PostgreSQL is used instead of SQLite.
5. Migrations are applied.
6. Static files load.
7. Media configuration works.
8. Django admin opens.
9. Login pages and forms render.
10. CSRF works through HTTPS.
11. The campaign/message pages render.
12. Starting a campaign does not fail because Redis or Celery is unavailable.
13. The application displays meaningful message-send status and does not falsely report success when the provider rejects a request.
14. API credentials load securely from environment variables.
15. No other deployed domain or service was changed or interrupted.
16. Nginx configuration passes `nginx -t`.
17. HTTPS works with a valid certificate.
18. `python manage.py check --deploy` has no unaddressed critical production errors.

Inspect logs during testing:

```bash
journalctl -u wsapp.service -n 200 --no-pager
tail -n 100 /var/log/nginx/error.log
```

Do not send multiple real WhatsApp trial messages.

If a live provider test is genuinely needed and credentials are available, send no more than one test message to the configured test number, only after validating the request payload locally. Clearly record whether the provider accepted or rejected the single request. Never expose the API key or token in logs or documentation.

## Phase 11: Idempotent deployment/update script

Create or improve:

```text
/var/www/wsapp/scripts/deploy_production.sh
```

Also copy the final safe version back into the repository under:

```text
/root/wsapp/wsapp/scripts/deploy_production.sh
```

The script should be idempotent and suitable for future updates. It should:

* use `set -Eeuo pipefail`;
* synchronize source code safely;
* preserve production environment files;
* preserve uploaded media;
* activate the virtual environment;
* install updated requirements;
* run migrations;
* collect static files;
* restart only `wsapp.service`;
* run `nginx -t`;
* reload Nginx only when valid;
* verify the service and HTTPS endpoint;
* exit with a non-zero status on failure;
* avoid touching unrelated services.

Do not put secrets in the script.

## Phase 12: Deployment documentation

Create detailed documentation at:

```text
/root/wsapp/wsapp/DEPLOYMENT.md
```

Copy it into:

```text
/var/www/wsapp/DEPLOYMENT.md
```

Document:

* architecture;
* source directory;
* production directory;
* domain;
* selected Gunicorn TCP port;
* systemd service path;
* Nginx configuration path;
* environment file path;
* PostgreSQL database and role names;
* virtual environment path;
* static and media paths;
* deployment/update procedure;
* restart commands;
* service-status commands;
* log commands;
* migration commands;
* static collection command;
* certificate-renewal verification;
* rollback procedure;
* backup considerations;
* troubleshooting for `502`, `500`, CSRF, static files, database errors, and message-provider errors;
* how to safely create a Django superuser;
* how to rotate API and database credentials.

Do not include passwords, tokens, secret keys, or full API credentials.

Also record all source-code modifications made for production deployment.

## Final response

After completing everything, provide a concise deployment report containing:

1. Deployment success or failure.
2. Final public URL.
3. Gunicorn loopback port selected.
4. Systemd service status.
5. PostgreSQL database and role names.
6. Nginx configuration status.
7. HTTPS certificate status and expiration information.
8. Django migration and static collection status.
9. Tests and smoke checks performed.
10. Files modified or created.
11. Any remaining non-critical warnings.
12. Exact commands for checking service and logs.

Do not claim success unless HTTPS responds correctly and `wsapp.service` is active.

If any step cannot be completed, continue with every other safe step, document the exact blocker, include relevant sanitized error output, and leave the server in a stable state.
