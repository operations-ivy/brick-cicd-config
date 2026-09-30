// Re-run the chucks-wisdom joke importer (delete its Job and apply it again).
pipeline {
    agent any
    options { disableConcurrentBuilds(); timestamps(); timeout(time: 30, unit: 'MINUTES') }
    stages {
        stage('Import') {
            steps { sh '/brick-cicd-config/brick9000/jenkins/bin/chuck-importer' }
        }
    }
}
