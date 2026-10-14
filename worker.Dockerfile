FROM python:3.11-slim

# One image, one role: the background worker. Same content as the API image
# (shared COPY steps over the same source tree) but its own build declaration so
# no compose service starts a listener and a job loop from one CMD: this file
# declares no EXPOSE and runs the scheduler instead of uvicorn.
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

RUN useradd --create-home --uid 10001 appuser \
 && mkdir -p /workspace/inbound \
 && chown -R appuser:appuser /workspace
USER appuser
CMD ["python", "-m", "src.workers.scheduler"]
