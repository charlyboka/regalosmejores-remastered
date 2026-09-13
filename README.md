# Regalos Mejores

Spanish gift-discovery site: natural-language search over a curated Amazon catalogue, monetised
with Amazon Associates.

Django 5 · PostgreSQL + pgvector · HTMX · Tailwind · deployed on Heroku.

- **Architecture and build plan:** [docs/project_blueprint.md](docs/project_blueprint.md)
- **How to run, monitor and troubleshoot it:** [docs/RUNBOOK.md](docs/RUNBOOK.md)

```powershell
uv sync
Copy-Item .env.example .env     # fill in the values
uv run python manage.py migrate
uv run python manage.py runserver
```
