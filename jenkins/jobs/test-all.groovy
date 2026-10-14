// Run every repo's tests (the test-<repo> jobs) on main, and fail naming the
// repos whose tests failed. Nightly at 21:00-21:29 local, while the board's
// lights are on to show it (brick-status' "nightly" pattern), done well
// before its quiet hours start at midnight. Jenkins runs on UTC, hence TZ.
// cron TZ=America/New_York
// cron H(0-29) 21 * * *
pipeline {
    agent none
    options { disableConcurrentBuilds(); timestamps(); timeout(time: 2, unit: 'HOURS') }
    stages {
        stage('Test') {
            steps {
                script {
                    def failed = []
                    // One at a time: brick2000 runs the cluster's volumes too.
                    for (repo in ['brick-cicd-config', 'wigle-sync', 'chucks-wisdom', 'overwatch']) {
                        def run = build(job: "test-${repo}", parameters: [string(name: 'BRANCH', value: 'main')],
                                        propagate: false, wait: true)
                        echo "test-${repo}: ${run.result}"
                        if (run.result != 'SUCCESS') {
                            failed << "${repo} (${run.result})"
                        }
                    }
                    if (failed) {
                        error("Tests failed: ${failed.join(', ')}")
                    }
                }
            }
        }
    }
}
