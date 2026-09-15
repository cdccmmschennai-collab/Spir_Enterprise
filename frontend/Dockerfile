# SPIR Dynamic Extraction — Next.js frontend image.
#
# NEXT_PUBLIC_* variables are inlined into the browser bundle by `next build`,
# so the API URL is a BUILD ARG, not a runtime env var. The browser (not the
# container) calls the API, so it must be a host-reachable URL — the Compose
# file passes http://localhost:8000.
#
# frontend/.dockerignore excludes .env.local (production URL) so it can never
# leak into a local image.

# ── Stage 1: install all dependencies (build needs devDependencies) ──────────
FROM node:22-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

# ── Stage 2: build ───────────────────────────────────────────────────────────
FROM node:22-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .

ARG NEXT_PUBLIC_API_URL=http://localhost:8000
ENV NEXT_PUBLIC_API_URL=${NEXT_PUBLIC_API_URL} \
    NEXT_TELEMETRY_DISABLED=1

RUN npm run build

# ── Stage 3: runtime (production deps only, same `next start` as PM2 uses) ───
FROM node:22-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production \
    PORT=3000 \
    NEXT_TELEMETRY_DISABLED=1

COPY package.json package-lock.json ./
RUN npm ci --omit=dev

COPY --from=builder /app/.next ./.next
COPY --from=builder /app/public ./public
COPY next.config.js ./

USER node
EXPOSE 3000

CMD ["npm", "run", "start"]
