# Tell the plasma API about an image build or deploy on brick9000: rainbow
# while an image builds, the buttons boiling while brick9000 deploys, then
# green or red. Sourced by build-image and deploy, which set $image and $ref
# first; deploy also sets status_name=deploy, so it's a sender of its own.
# Lights are never worth failing a deploy for: every call gives up quickly.
plasma_url=${BRICK_PLASMA_URL:-http://127.0.0.1:8766}
plasma_source=brick9000-${status_name:-image}

plasma_signal() {  # <pattern> <ttl>
    curl -s -o /dev/null --max-time 1 -X PUT -H 'Content-Type: application/json' \
        -d "{\"pattern\": \"$1\", \"ttl\": $2}" \
        "$plasma_url/signals/$plasma_source" || true
}

write_status() {  # <building|succeeded|failed>
    case $1 in
        # The time limit covers a build killed outright: it never says it ended.
        building) if [ "${status_name:-}" = deploy ]; then plasma_signal deploying 900
                  else plasma_signal image-building 3600; fi ;;
        succeeded) plasma_signal image-pushed 60 ;;
        failed) plasma_signal image-failed 60 ;;
    esac
}
