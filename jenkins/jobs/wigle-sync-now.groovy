// Run wigle-sync now from its CronJob, instead of waiting for the top of the hour.
pipeline {
    agent { label 'built-in' }
    options { disableConcurrentBuilds(); timestamps(); timeout(time: 60, unit: 'MINUTES') }
    stages {
        stage('Sync') {
            steps { sh '/usr/share/brick-jenkins/bin/wigle-sync-now' }
        }
    }
}
