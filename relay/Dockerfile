FROM node:22-alpine

WORKDIR /app

# deps first so layer caching works
COPY package.json package-lock.json* ./
RUN npm install --omit=dev --no-audit --no-fund

COPY server.js ./
COPY lib ./lib
COPY public ./public

# device registry, operators and audit log live here -- mount a volume
VOLUME ["/data"]
ENV AEGIS_DATA_DIR=/data \
    PORT=7443 \
    NODE_ENV=production

EXPOSE 7443

HEALTHCHECK --interval=30s --timeout=4s --start-period=8s \
  CMD wget -qO- http://127.0.0.1:${PORT}/healthz || exit 1

# don't run as root
RUN addgroup -S aegis && adduser -S aegis -G aegis && mkdir -p /data && chown -R aegis:aegis /data /app
USER aegis

CMD ["node", "server.js"]
