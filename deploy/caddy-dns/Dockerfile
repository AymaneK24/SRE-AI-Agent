# Caddy with the Azure DNS plugin, for Let's Encrypt certificates on a private server (DNS-01 challenge).
# Let's Encrypt checks a temporary DNS record instead of connecting to the server, so nothing needs to be open
# to the internet. Build it once, then set RCC_CADDY_IMAGE=rcc-caddy-dns:2 in .env:
#
#   docker build --pull -t rcc-caddy-dns:2 deploy/caddy-dns
#
FROM caddy:2-builder-alpine AS build
RUN xcaddy build --with github.com/caddy-dns/azure

FROM caddy:2-alpine
COPY --from=build /usr/bin/caddy /usr/bin/caddy
