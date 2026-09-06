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
# The entrypoint substitutes exactly two variables into the rendered config: CORE_APP_URL and
# NGINX_RESOLVER. The filter is not cosmetic — without it envsubst would eat nginx's own
# $host / $core / $proxy_add_x_forwarded_for, silently emptying the proxy headers.
COPY services/web/nginx.conf.template /etc/nginx/templates/default.conf.template
# Sourced before the template is rendered (.envsh, hence executable): derives NGINX_RESOLVER
# from the container's /etc/resolv.conf, so the same image proxies the core under Compose
# (Docker's embedded DNS) and in a Kubernetes pod (cluster DNS) — #891.
COPY services/web/docker-entrypoint.d/05-epicurus-resolver.envsh /docker-entrypoint.d/
COPY --from=build /app/dist /usr/share/nginx/html
ENV CORE_APP_URL=http://core-app:8080
ENV NGINX_ENVSUBST_FILTER="^(CORE_APP_URL|NGINX_RESOLVER)\$"
EXPOSE 8080
