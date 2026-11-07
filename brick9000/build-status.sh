# Tell the plasma API about an image build or deploy on brick9000: rainbow
# while an image builds, the buttons boiling while brick9000 deploys, then
# green or red. Sourced by deploy, which sets $image, $ref and status_name=deploy first.
# Lights are never worth failing a deploy for: every call gives up quickly.
plasma_url=${BRICK_PLASMA_URL:-http://127.0.0.1:8766}
plasma_source=brick9000-${status_name:-image}

plasma_signal() {  # <pattern> <ttl> <status for the board's CRT>
    curl -s -o /dev/null --max-time 1 -X PUT -H 'Content-Type: application/json' \
        -d "{\"pattern\": \"$1\", \"ttl\": $2, \"show\": {\"title\": \"${status_name:+$status_name }$image\", \"status\": \"$3\", \"details\": {\"commit\": \"$ref\"}}}" \
        "$plasma_url/signals/$plasma_source" || true
}

write_status() {  # <building|succeeded|failed>
    case $1 in
        # The time limit covers a build killed outright: it never says it ended.
        building) if [ "${status_name:-}" = deploy ]; then plasma_signal deploying 900 deploying
                  else plasma_signal image-building 3600 building; fi ;;
        succeeded) plasma_signal image-pushed 60 succeeded ;;
        failed) plasma_signal image-failed 60 failed ;;
    esac
}
