FROM node:24-alpine AS web
WORKDIR /build
COPY apps/web/package*.json ./
RUN npm ci
COPY apps/web/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml requirements-lock.txt ./
COPY repopilot/ ./repopilot/
RUN pip install --no-cache-dir -r requirements-lock.txt && pip install --no-cache-dir --no-deps .
COPY examples/ ./examples/
COPY eval/ ./eval/
COPY --from=web /build/dist ./apps/web/dist
RUN useradd --create-home --uid 10001 repopilot && mkdir /app/runtime && chown repopilot:repopilot /app/runtime
USER repopilot
EXPOSE 8765
CMD ["python", "-m", "repopilot", "serve", "--host", "0.0.0.0"]
