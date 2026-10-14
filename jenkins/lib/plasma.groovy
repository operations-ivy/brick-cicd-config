// The plasma library, shared by every job: seed.groovy puts this file ahead
// of each jobs/<name>.groovy, so its functions are part of every pipeline.
//
//   steps { script { plasma('test') { sh 'poetry run pytest' } } }
//
// marks the build with what it's doing while the block runs. brick-status on
// brick9000 polls Jenkins (as it already does for the Builds view), reads the
// mark from each running build, and plays that kind's pattern on the cabinet's
// buttons; the build's result then shows as usual. A pull, not a push: it
// works from agent pods, which can't reach brick9000, and opens no way in.
//
// Kinds (brick-status' patterns.STAGES): checkout, install, test, build, push,
// deploy, verify. The mark is the build's description, "plasma:<kind>", and
// the description before the block comes back after it.

def plasma(String kind, Closure body) {
    def kinds = ['checkout', 'install', 'test', 'build', 'push', 'deploy', 'verify']
    if (!(kind in kinds)) {
        error("plasma: unknown kind '${kind}' (one of ${kinds.join(', ')})")
    }
    def before = currentBuild.description
    currentBuild.description = "plasma:${kind}"
    try {
        return body()
    } finally {
        currentBuild.description = before
    }
}
