# Base image pinned to the one the commits were labeled in.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ENV PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

# Same packages the commits were labeled with, so sqlglot's tests behave the same.
COPY requirements.lock /app/requirements.lock
RUN pip install -r /app/requirements.lock

# Fetch sqlglot at the pinned commit, run its suite once with per-test coverage,
# and keep only the compressed map. Nothing here is charged at run time.
COPY covplugin.py build_map.py build.sh /app/
RUN sh /app/build.sh

COPY selector.py /app/selector.py
CMD ["python", "/app/selector.py"]
