FROM python:3.11-slim

# One image, one role. This Dockerfile builds the API listener: the worker role
# builds worker.Dockerfile (its own CMD for src.workers.scheduler and no EXPOSE),
# while Dockerfile.lint and Dockerfile.test serve the lint and unit-test roles, so
# no compose service that builds a file is asked to start two roles from one CMD.
#
# PYTHONPATH starts at the image root so that the CMD below can resolve the
# top-level `src` package without the package being pip-installed: the console
# script `uvicorn` does not put the working directory on sys.path. The same image
# builds the API listener; the `worker` service builds worker.Dockerfile instead.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    NO_CACHE_DIR=1 \
    PYTHONPATH=/workspace:/workspace/src:/workspace/tests

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

COPY requirements.txt ./
RUN pip install --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
# migrations/ is carried so the schema this image's repositories read is the
# schema this image was built from; config/ so the integration configuration the
# worker and the JDE/bottomline clients read travels with the code. Neither is
# build-time only: `migrate` mounts migrations/ from the host, and ops inspect
# config/ inside the running container.
COPY migrations/ ./migrations/
COPY config/ ./config/
# The test suite is NOT copied here. `tests/` belongs to Dockerfile.test, which
# copies src/ and tests/ itself, so the production image ships no tests while
# the suite stays runnable. conftest.py and pytest.ini stay at the image root
# so `python -m pytest` against a mounted tests volume keeps its suite options
# (--localstack, --api-base-url) instead of aborting with 'unrecognized
# arguments' during argument parsing.
COPY conftest.py ./conftest.py
COPY pytest.ini ./pytest.ini
COPY pyproject.toml ./

# Runtime identity. Everything above needs root (apt and pip write into system
# prefixes); nothing below does, so the image drops to an unprivileged account
# before the service is ever started. The account owns /workspace so the API can
# still read its own configuration, migration and package trees, and it needs no
# shell. `migrate` keeps its own postgres:16-alpine image and is unaffected; the
# bind-mounted migrations/ and config/ trees stay world-readable, which is what
# the non-root process relies on.
RUN useradd --system --create-home --uid 10001 --shell /usr/sbin/nologin coaapp \
 && chown -R coaapp:coaapp /workspace

USER coaapp

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=10s --start-period=30s --retries=12 \
  CMD python -c "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5).status == 200 else 1)"

CMD ["uvicorn", "src.api.mule.endpoints:app", "--host", "0.0.0.0", "--port", "8000"]
