// Deploy brick1982's LCARS panel (operations-ivy/rpi_lcars) from a branch now,
// instead of waiting for its 2-hourly timer, which then keeps following that
// branch. Leave BRANCH empty to go back to master (after merging, say).
// brick1982's clone of this repo (host tooling) just follows main.
// param BRANCH= Branch of rpi_lcars to deploy and follow (empty: master)
pipeline {
    agent { label 'built-in' }
    options { disableConcurrentBuilds(); timestamps(); timeout(time: 5, unit: 'MINUTES') }
    stages {
        stage('Deploy') {
            steps { sh '/usr/share/brick-jenkins/bin/deploy-brick1982' }
        }
    }
}
