FROM node:22-alpine

WORKDIR /app

COPY node_gateway/package.json ./package.json
COPY node_gateway/server.js ./server.js
COPY node_gateway/openapi.json ./openapi.json

ENV NODE_ENV=production \
    PORT=8080 \
    PYTHON_API_BASE_URL=http://api:8000

EXPOSE 8080

CMD ["node", "server.js"]
