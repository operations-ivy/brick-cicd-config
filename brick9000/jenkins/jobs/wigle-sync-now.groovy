// Run wigle-sync now from its CronJob, instead of waiting for the top of the hour.
pipeline {
    agent any
    options { disableConcurrentBuilds(); timestamps(); timeout(time: 60, unit: 'MINUTES') }
    stages {
        stage('Sync') {
            steps { sh '/brick-cicd-config/brick9000/jenkins/bin/wigle-sync-now' }
        }
    }
}
