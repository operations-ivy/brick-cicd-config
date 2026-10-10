// Deploy brick9000 from a branch of brick-cicd-config now, instead of waiting
// for its 2-hourly timer, which then keeps following that branch. Leave BRANCH
// empty to go back to main (after merging, say).
// param BRANCH= Branch to deploy and follow (empty: main)
pipeline {
    agent { label 'built-in' }
    options { disableConcurrentBuilds(); timestamps(); timeout(time: 5, unit: 'MINUTES') }
    stages {
        stage('Deploy') {
            steps { sh '/usr/share/brick-jenkins/bin/deploy-brick9000' }
        }
    }
}
