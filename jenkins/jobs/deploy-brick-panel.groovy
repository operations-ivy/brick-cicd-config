// Deploy brick1982's neon panel (operations-ivy/brick-panel) from a branch
// now, instead of waiting for its 2-hourly timer, which then keeps following
// that branch. Leave BRANCH empty to go back to main (after merging, say).
// param BRANCH= Branch of brick-panel to deploy and follow (empty: main)
pipeline {
    agent { label 'built-in' }
    options { disableConcurrentBuilds(); timestamps(); timeout(time: 5, unit: 'MINUTES') }
    stages {
        stage('Deploy') {
            steps { script { plasma('deploy') { sh '/usr/share/brick-jenkins/bin/deploy-brick-panel' } } }
        }
    }
}
