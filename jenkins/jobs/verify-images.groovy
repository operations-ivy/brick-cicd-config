// Check every app image is reproducible: for each, take Docker Hub's newest
// tag, read the commit its revision label names, rebuild that commit from
// scratch (--no-cache) the way build-image built it, and fail naming any
// image whose digest differs, or that has no label to rebuild from. Nightly
// at 21:30-21:59 local, after test-all, while the board's lights are on.
// cron TZ=America/New_York
// cron H(30-59) 21 * * *
pipeline {
    agent { kubernetes { yaml buildkitPod() } }
    options { disableConcurrentBuilds(); timestamps(); timeout(time: 3, unit: 'HOURS') }
    stages {
        stage('Verify') {
            steps {
                script {
                    def problems = []
                    // One at a time: a no-cache build is a few CPU-minutes on brick2000.
                    for (image in brickImages().keySet().sort()) {
                        def info = ''
                        container('tools') {
                            info = sh(script: "/usr/share/brick-jenkins/bin/hub-image-info --kv '${image}'", returnStdout: true).trim()
                        }
                        def hub = [:]
                        for (line in info.readLines()) {
                            def eq = line.indexOf('=')
                            if (eq > 0) {
                                hub[line.substring(0, eq)] = line.substring(eq + 1)
                            }
                        }
                        if (hub.exists != 'true' || !hub.revision) {
                            problems << "${image}:${hub.tag ?: '?'} has no revision label (republish it with build-image)"
                            continue
                        }
                        def dir = "src-${image.split('/')[1]}"
                        def got = checkoutForBuild(image, hub.revision, dir)
                        def rebuilt = buildImage(image, hub.tag, dir, got[0], got[1], false, true)
                        if (rebuilt == hub.digest) {
                            echo "verify-images: ${image}:${hub.tag} OK, ${rebuilt}"
                        } else {
                            problems << "${image}:${hub.tag} rebuilds to ${rebuilt}, Hub has ${hub.digest}"
                        }
                    }
                    if (problems) {
                        error("verify-images:\n" + problems.join('\n'))
                    }
                }
            }
        }
    }
}
