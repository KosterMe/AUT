# Build context is ./webapp (see docker-compose.yml).
FROM node:20-alpine AS build
WORKDIR /app

# pnpm, because pnpm-lock.yaml is this project's lockfile. `npm install`
# ignored it and re-resolved the whole tree from the registry on every build —
# which is what made this image take tens of minutes and never finish.
RUN npm install -g pnpm@9.15.9

# Playwright is a devDependency because the editor is checked in a real
# browser (§8.5), and its install step downloads a few hundred megabytes of
# them. This image builds static files and never opens a page, so it takes the
# package and skips the browsers — otherwise every image build pays for a
# check that only ever runs on a developer's machine.
ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1

# Manifests first so a source edit does not re-resolve dependencies.
COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

COPY . .
RUN pnpm run build

FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
