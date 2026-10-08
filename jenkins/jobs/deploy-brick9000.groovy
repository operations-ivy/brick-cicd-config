// Start brick9000's deploy now, instead of waiting for its 2-hourly timer.
pipeline {
    agent { label 'built-in' }
    options { disableConcurrentBuilds(); timestamps(); timeout(time: 5, unit: 'MINUTES') }
    stages {
        stage('Deploy') {
            steps { sh '/usr/share/brick-jenkins/bin/deploy-brick9000' }
        }
    }
}
