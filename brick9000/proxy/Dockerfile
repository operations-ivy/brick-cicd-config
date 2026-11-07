# Caddy with the Porkbun DNS provider, for the Let's Encrypt DNS challenge
# behind *.brick.nozdormu.cloud's certificate: whitepatrick/brick-proxy, the
# cluster's front door. Built on brick2000's BuildKit (brick-k8s-config README,
# "Front door on the cluster"); change a version here, build a new tag, pin it.
ARG CADDY_VERSION=2.11.4

FROM caddy:${CADDY_VERSION}-builder-alpine AS build
ARG CADDY_VERSION
RUN xcaddy build "v${CADDY_VERSION}" --with github.com/caddy-dns/porkbun@v0.3.1

FROM caddy:${CADDY_VERSION}-alpine
COPY --from=build /usr/bin/caddy /usr/bin/caddy
