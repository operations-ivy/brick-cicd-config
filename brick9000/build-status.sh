# Record an image build or deploy on brick9000 for brick-status' lights
# (rainbow while an image builds, the buttons boiling while brick9000 deploys,
# then green or red; see brick-status/brick_status/build.py). Sourced by
# build-image, jenkins/up and deploy, which set $image and $ref first; deploy
# also sets status_name=deploy for a file of its own.
build_state=${XDG_STATE_HOME:-$HOME/.local/state}/brick-build
status_file=$build_state/${status_name:-status}.json
mkdir -p "$build_state"
started=$(date +%s)

# One JSON line per state change, replaced atomically so brick-status never
# reads half a file.
write_status() {  # <building|succeeded|failed> [finished]
    printf '{"state": "%s", "image": "%s", "ref": "%s", "pid": %d, "started": %d%s}\n' \
        "$1" "$image" "$ref" $$ "$started" "${2:+, \"finished\": $2}" >"$status_file.tmp"
    mv "$status_file.tmp" "$status_file"
}
