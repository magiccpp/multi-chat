#!/bin/sh
# Wait for Postgres (compose `depends_on` health conditions are not honoured by every
# compose implementation, e.g. nerdctl), apply migrations, then start the server.
set -e

python - <<'PY'
import asyncio, os, sys, time
import asyncpg

url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
deadline = time.monotonic() + int(os.environ.get("DB_WAIT_SECONDS", "90"))

async def main():
    while True:
        try:
            conn = await asyncpg.connect(url, timeout=5)
            await conn.close()
            return
        except Exception as exc:
            if time.monotonic() > deadline:
                print(f"database not reachable: {exc}", file=sys.stderr)
                sys.exit(1)
            print(f"waiting for database: {exc}", file=sys.stderr)
            await asyncio.sleep(2)

asyncio.run(main())
PY

alembic upgrade head
exec "$@"
