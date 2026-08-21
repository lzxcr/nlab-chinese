# --- syntax: docker/dockerfile ---
# nLab-Chinese static site image.
#
# Stage 1: build the site (pnpm + node 22)
# Stage 2: serve public/ with a minimal non-root static server.

FROM node:22-slim AS builder
WORKDIR /usr/src/app

# pnpm via corepack (package.json pins the version).
RUN corepack enable
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile

COPY . .
ARG NLAB_SITE_URL=nlab-chinese.github.io
ARG NLAB_REPOSITORY_URL=""
RUN NLAB_SITE_URL="$NLAB_SITE_URL" NLAB_REPOSITORY_URL="$NLAB_REPOSITORY_URL" \
    NLAB_OG_IMAGES=0 pnpm quartz build

FROM nginxinc/nginx-unprivileged:1.30-alpine AS runner
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /usr/src/app/public /usr/share/nginx/html

EXPOSE 8080
