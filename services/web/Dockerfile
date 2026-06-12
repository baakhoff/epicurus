# Build the static shell, then serve it from an unprivileged nginx that also
# reverse-proxies /platform/ to the core (same-origin: no CORS anywhere).
# Build context is the repo root (like every service).

FROM node:22-alpine AS build
WORKDIR /app
COPY services/web/package.json services/web/package-lock.json ./
RUN npm ci
COPY services/web/ ./
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.27-alpine
# The entrypoint substitutes ${CORE_APP_URL} into the rendered config.
COPY services/web/nginx.conf.template /etc/nginx/templates/default.conf.template
COPY --from=build /app/dist /usr/share/nginx/html
ENV CORE_APP_URL=http://core-app:8080
EXPOSE 8080
