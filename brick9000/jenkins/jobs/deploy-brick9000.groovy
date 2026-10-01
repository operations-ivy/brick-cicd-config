// Start brick9000's deploy now, instead of waiting for its 2-hourly timer.
pipeline {
    agent any
    options { disableConcurrentBuilds(); timestamps(); timeout(time: 5, unit: 'MINUTES') }
    stages {
        stage('Deploy') {
            steps { sh '/brick-cicd-config/brick9000/jenkins/bin/deploy-brick9000' }
        }
    }
}
