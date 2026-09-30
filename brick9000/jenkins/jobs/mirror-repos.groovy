// Keep local clones of brick-k8s-config, wigle-sync and chucks-wisdom current, so
// the other jobs work from them even when GitHub or the internet is down.
// cron H */2 * * *
pipeline {
    agent any
    options { disableConcurrentBuilds(); timestamps(); timeout(time: 15, unit: 'MINUTES') }
    stages {
        stage('Mirror') {
            steps { sh '/brick-cicd-config/brick9000/jenkins/bin/mirror-repos' }
        }
    }
}
