FROM node:22-alpine
WORKDIR /app

COPY package.json package-lock.json* ./
RUN npm ci --ignore-scripts

COPY . .
RUN npm run build && npm prune --production && npm cache clean --force

EXPOSE 8001
CMD ["node", "dist/index.js"]
