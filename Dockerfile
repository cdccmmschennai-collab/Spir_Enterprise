# SPIR Dynamic Extraction — backend image (FastAPI API, Celery workers, Celery Beat).
#
# One image serves every Python service; docker-compose.yml overrides `command`
# per service. The project root is /opt/spir_dynamic — the same path production
# uses — so config.py's _PROJECT_ROOT (4 levels above src/spir_dynamic/app/config.py)
# and the CWD-relative storage/avatars path both resolve exactly as in production.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # Makes `import spir_dynamic` work from the source tree without pip-installing
    # the package (keeps _PROJECT_ROOT anchored at /opt/spir_dynamic).
    PYTHONPATH=/opt/spir_dynamic/src

WORKDIR /opt/spir_dynamic

# Dependencies first so source edits don't invalidate the pip layer.
COPY requirements.txt ./
RUN pip install -r requirements.txt

# Application source + runtime config. .env files are excluded by .dockerignore —
# all settings come from the Compose environment.
COPY src ./src
COPY config ./config
COPY app.py worker.py pyproject.toml ./

# Run as an unprivileged user (production runs as `spir`). The storage tree is
# created here so the named volume inherits its ownership on first creation.
RUN useradd --system --create-home --shell /usr/sbin/nologin spir \
    && mkdir -p storage/extracted_rows storage/batch_uploads storage/avatars \
    && chown -R spir:spir /opt/spir_dynamic
USER spir

EXPOSE 8000

# Default = API. Workers/Beat override this in docker-compose.yml.
CMD ["uvicorn", "spir_dynamic.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
