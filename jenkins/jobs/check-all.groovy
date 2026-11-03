// Check the whole homelab now: scripts/smoke (is everything up?), then
// test-all (every repo's tests on main). Started by pressing brick9000's
// front-right button twice (brick-status/brick_status/check.py). The lights
// show the stages and test-all's nightly pattern, then green for a pass or red
// for a failure. Read-only: it changes nothing anywhere.
pipeline {
    agent { label 'built-in' }
    options { disableConcurrentBuilds(); timestamps(); timeout(time: 2, unit: 'HOURS') }
    stages {
        stage('Smoke') {
            steps {
                script {
                    plasma('verify') {
                        // A failure here still runs the tests: both are worth knowing.
                        catchError(buildResult: 'FAILURE', stageResult: 'FAILURE') {
                            sh '/usr/share/brick-jenkins/bin/smoke'
                        }
                    }
                }
            }
        }
        stage('Test') {
            steps { build(job: 'test-all', wait: true, propagate: true) }
        }
    }
    post {
        success { script { plasmaResult('check-passed', 30) } }
        unsuccessful { script { plasmaResult('check-failed', 300) } }
    }
}
