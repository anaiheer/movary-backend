# Movary Backend

Backend for Movary — a self-hosted media server management platform (Emby / MoviePilot).
FastAPI + SQLAlchemy + Alembic + Redis. This is the open-source base edition; the paid Pro
extension plugs into it through the extension contract without modifying this codebase.

## Quick start

The fastest way to run the full stack (Postgres + Redis + backend + frontend) is the
[Movary deploy repo](https://github.com/anaiheer/Movary):

```bash
git clone https://github.com/anaiheer/Movary.git
cd Movary
docker compose up -d
```

The published image is `movary/movary-backend` on Docker Hub.

## Development

Requires Python 3.11.

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt -r requirements-dev.txt ruff

ruff format --check app tests
ruff check app tests
pytest tests -v
```

Tests run against SQLite; production uses PostgreSQL (asyncpg) and Redis.

## Configuration

Copy `.env.example` to `.env` and adjust. Key variables include `DATABASE_URL`, `REDIS_URL`,
`SECRET_KEY`, `EMBY_PASSWORD_KEY`, and `TMDB_API_KEY`.
## License

[GNU Affero General Public License v3.0](LICENSE)
