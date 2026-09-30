# Record an image build on brick9000 for brick-status' lights (rainbow while
# building, then green or red; see brick-status/brick_status/build.py).
# Sourced by build-image and jenkins/up, which set $image and $ref first.
build_state=${XDG_STATE_HOME:-$HOME/.local/state}/brick-build
mkdir -p "$build_state"
started=$(date +%s)

# One JSON line per state change, replaced atomically so brick-status never
# reads half a file.
write_status() {  # <building|succeeded|failed> [finished]
    printf '{"state": "%s", "image": "%s", "ref": "%s", "pid": %d, "started": %d%s}\n' \
        "$1" "$image" "$ref" $$ "$started" "${2:+, \"finished\": $2}" >"$build_state/status.json.tmp"
    mv "$build_state/status.json.tmp" "$build_state/status.json"
}
