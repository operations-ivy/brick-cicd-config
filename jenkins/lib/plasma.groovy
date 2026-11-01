// The plasma library, shared by every job: seed.groovy puts this file ahead
// of each jobs/<name>.groovy, so its functions are part of every pipeline.
//
//   steps { script { plasma('test') { sh 'poetry run pytest' } } }
//
// plays that kind's pattern on the cabinet's buttons while the block runs, by
// telling the plasma API (brick-plasma/) at once, so a step of a few seconds
// still shows. Each job is one sender. Its signal stays a moment after the
// block ends, which lets the next stage take over with no flicker of the
// board's own pattern, and the time limit clears the lights if the build dies.
//
// Lights are never worth failing or slowing a build for: a call gives up after
// a second, and brick9000 being down costs only a line in the build's log.
//
// Kinds (brick-plasma's patterns.STAGES): checkout, install, test, build, push,
// deploy, verify. The block also marks the build's description "plasma:<kind>",
// which the board's Builds view shows; the description before it comes back after.

def plasmaSignal(String kind, int ttl) {
    try {
        httpRequest(url: "http://plasma.brick.nozdormu.cloud/signals/jenkins-${env.JOB_NAME}",
                    httpMode: 'PUT', contentType: 'APPLICATION_JSON',
                    requestBody: "{\"pattern\": \"stage-${kind}\", \"ttl\": ${ttl}}",
                    timeout: 1, quiet: true, consoleLogResponseBody: false, validResponseCodes: '100:599')
    } catch (Exception e) {
        echo "plasma: no lights (${e.message})"
    }
}

def plasma(String kind, Closure body) {
    def kinds = ['checkout', 'install', 'test', 'build', 'push', 'deploy', 'verify']
    if (!(kind in kinds)) {
        error("plasma: unknown kind '${kind}' (one of ${kinds.join(', ')})")
    }
    def before = currentBuild.description
    currentBuild.description = "plasma:${kind}"
    plasmaSignal(kind, 3600)  // the API's longest; a longer stage falls back to the board's chase
    try {
        return body()
    } finally {
        currentBuild.description = before
        plasmaSignal(kind, 2)
    }
}
