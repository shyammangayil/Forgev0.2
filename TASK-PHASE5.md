# TASK-PHASE5 — Deployment & Packaging

Parent: [TASK.md](TASK.md)

---

- [ ] `requirements.txt` — all dependencies with pinned versions
- [ ] `requirements-dev.txt` — test/dev dependencies
- [ ] `.env.example` — every environment variable, commented, with safe defaults
- [ ] `Dockerfile` — multi-stage build, non-root user, health check
- [ ] `docker-compose.yml` — forge + postgres + redis + prometheus + grafana
- [ ] `docker-compose.minimal.yml` — forge only, SQLite, no extras
- [ ] `pyproject.toml` — modern packaging, entry points for `forge` CLI command
- [ ] `scripts/setup.sh` — one-command local setup (venv, deps, .env copy)
- [ ] `scripts/run_tests.sh` — runs full test suite with coverage report
- [ ] `helm/Chart.yaml` + `helm/values.yaml` — Kubernetes Helm chart skeleton
- [ ] `.github/workflows/ci.yml` — lint + test + build on push

## Acceptance Criteria
- [ ] `bash scripts/setup.sh` works on a clean machine
- [ ] `docker-compose up` starts all services
- [ ] `docker-compose.minimal.yml` starts with just `ANTHROPIC_API_KEY` set
- [ ] `pip install -e .` makes `forge` available as a CLI command
- [ ] CI pipeline passes on GitHub
