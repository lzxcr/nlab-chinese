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
RUN pnpm quartz build

FROM node:22-slim AS runner
WORKDIR /usr/src/app
ENV NODE_ENV=production

COPY --from=builder /usr/src/app/public ./public
COPY --from=builder /usr/src/app/node_modules ./node_modules
COPY --from=builder /usr/src/app/package.json ./package.json
# Minimal static file server using the already-installed serve-handler.
COPY server.mjs ./server.mjs

# Never run as root.
RUN useradd --create-home --uid 10001 nlabuser
USER nlabuser

EXPOSE 8080
CMD ["node", "server.mjs"]
