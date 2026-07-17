# Implementation plan

1. Harden environment-driven Django, storage, database, Redis and Celery setup.
2. Add normalized UUID models with explicit ownership, audit attempts, suppression,
   provider webhook idempotency, and safe admin registration.
3. Implement import, Tanzania normalization, template rendering, Wasender client,
   rate limiting, media lifecycle, webhook state mapping, exports, and tasks.
4. Add authenticated HTML/AJAX workflows and a shared Purple-inspired Bootstrap UI.
5. Add migrations and focused automated tests; run migrations, checks, and tests.
6. Keep the live command double-gated and perform at most one check plus one send.
