# Implementation plan

1. Harden Django, storage, and database setup.
2. Add normalized UUID models with explicit ownership, audit attempts,
   suppression, and safe admin registration.
3. Implement import, Tanzania normalization, template rendering, Wasender client,
   rate limiting, media lifecycle, exports, and synchronous services.
4. Add authenticated HTML/AJAX workflows and a shared Purple-inspired Bootstrap UI.
5. Add migrations and focused automated tests; run migrations, checks, and tests.
6. Keep the live command double-gated and process at most one recipient per request.
