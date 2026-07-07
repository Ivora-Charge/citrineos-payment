# Stage 1: Build the Frontend
FROM node:20 AS frontend-builder

WORKDIR /app/frontend

COPY frontend ./
RUN npm install
# DISABLE_ESLINT_PLUGIN: the CRA build's inline eslintConfig still extends the
# legacy "react-app" preset (not installed); linting runs separately via
# eslint.config.mjs. CI=false keeps warnings from failing the build.
RUN DISABLE_ESLINT_PLUGIN=true CI=false npm run build

# Stage 2: Create the Python Application
FROM python:3.10-slim

WORKDIR /app
COPY . /app

# Configuration comes from the environment (docker compose `environment:` or
# an env_file); config.py's load_dotenv tolerates a missing .env and never
# overrides variables already set in the environment. .env is dockerignored so
# secrets are not baked into the image.
RUN pip install --no-cache-dir --upgrade -r requirements.txt

RUN rm -rf ./frontend
COPY --from=frontend-builder /app/frontend/build /app/frontend/build

ENV WEBSERVER_HOST="0.0.0.0"
ENV WEBSERVER_PORT=9010
EXPOSE $WEBSERVER_PORT

CMD uvicorn main:app --host "$WEBSERVER_HOST" --port "$WEBSERVER_PORT"
